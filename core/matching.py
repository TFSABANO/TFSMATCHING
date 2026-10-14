"""Motore di abbinamento movimenti bancari <-> fatture / corrispettivi.

Regole (punteggio 0-100):

Fatture (incassi su fatture attive, pagamenti su fatture passive; note di credito al contrario)
  +50  importo uguale al residuo della fattura (± tolleranza)
  +35  numero fattura presente nella causale
  +25  ragione sociale della controparte nella causale (+10 se solo in parte)
  +10  data movimento vicina alla scadenza (±10 gg), +5 se entro 60 gg dalla fattura
  Più fatture della stessa controparte possono essere pagate con un unico bonifico:
  si cercano combinazioni (fino a 4 fatture) che sommano esattamente all'importo.

Corrispettivi elettronici (carte/POS) su accrediti:
  il POS (es. SumUp) accredita il netto dopo 1-3 giorni lavorativi, a volte cumulando più
  giorni: si cercano uno o più giorni consecutivi il cui lordo meno una commissione
  plausibile (0 - COMMISSIONE_POS_MAX_PCT %) corrisponde all'accredito.

Le proposte vengono assegnate in modo "greedy" dal punteggio più alto, così che lo stesso
documento non venga proposto su due movimenti diversi.
"""
import itertools
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from .models import Abbinamento, Corrispettivo, Fattura, MovimentoBancario

PAROLE_VUOTE = {
    "srl", "srls", "spa", "snc", "sas", "sapa", "scarl", "soc", "coop", "societa", "di", "de", "del", "della",
    "e", "ed", "and", "the", "ltd", "gmbh", "inc", "unipersonale", "semplificata", "group", "italia", "s.r.l.",
    "s.p.a.", "s.n.c.", "s.a.s.", "studio", "ditta",
}
PAROLE_POS = ["sumup", "payout", "pos", "accredito carte", "incasso carte", "card", "settlement", "auszahlung",
              "transazioni", "liquidazione"]


@dataclass
class Proposta:
    movimento: MovimentoBancario
    documenti: list  # [(documento, quota_movimento, quota_documento)]
    punteggio: int
    motivi: list = field(default_factory=list)


def _cfg(chiave):
    return settings.RICONCILIA[chiave]


def _testo(s):
    s = (s or "").lower()
    for a, b in (("à", "a"), ("è", "e"), ("é", "e"), ("ì", "i"), ("ò", "o"), ("ù", "u")):
        s = s.replace(a, b)
    return s


def _token_nome(nome):
    parole = re.findall(r"[a-z0-9]+", _testo(nome))
    return [p for p in parole if len(p) >= 3 and p not in PAROLE_VUOTE]


def punteggio_nome(nome, descrizione):
    token = _token_nome(nome)
    if not token:
        return 0
    descr = set(re.findall(r"[a-z0-9]+", _testo(descrizione)))
    trovati = sum(1 for t in token if t in descr)
    if trovati and trovati / len(token) >= 0.5:
        return 25
    return 10 if trovati else 0


def _progressivo(parti):
    """Parte progressiva di un numero fattura: '2026/15' -> '15', 'FPR 7/26' -> '7', 'A-77' -> '77'."""
    numeriche = [p for p in parti if p.isdigit()]
    if not numeriche:
        return max(parti, key=len)
    senza_anno = [p for p in numeriche if not re.fullmatch(r"(19|20)\d{2}", p)] or numeriche
    if len(senza_anno) > 1 and re.fullmatch(r"[2-4]\d", senza_anno[-1]):
        senza_anno = senza_anno[:-1]  # '7/26': anno a due cifre in coda
    return senza_anno[0]


def numero_in_descrizione(numero, descrizione):
    """Cerca il numero fattura nella causale.

    '2026/123' cerca anche '123/2026', '2026-123', '2026123'; numeri brevi (es. '7')
    vengono accettati solo se preceduti da 'fatt', 'ft', 'fattura n', 'nr', 'inv', 'rif'.
    """
    if not numero:
        return False
    descr = _testo(descrizione)
    num = _testo(numero).strip()
    parti = re.findall(r"[a-z0-9]+", num)
    if not parti:
        return False
    varianti = {num, "".join(parti), "/".join(parti), "-".join(parti), "/".join(reversed(parti))}
    principale = _progressivo(parti)
    for v in varianti:
        if len(v) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", descr):
            return True
    # numero breve o parte progressiva: richiede un prefisso esplicito
    progressivo = principale.lstrip("0") or principale
    prefissi = r"(?:fatt(?:ura|ure)?|ft|fat|fattura n|nr|n|num|inv(?:oice)?|rif|doc|rechnung)"
    return bool(re.search(rf"(?<![a-z]){prefissi}\.?\s*(?:n\.?|nr\.?|no\.?)?\s*0*{re.escape(progressivo)}(?![0-9])", descr))


def _punteggio_fattura(mov, fattura, residuo, tol):
    importo = abs(mov.importo)
    motivi, punti = [], 0
    if abs(residuo - importo) <= tol:
        punti += 50
        motivi.append("importo esatto")
    num = numero_in_descrizione(fattura.numero, mov.descrizione)
    if num:
        punti += 35
        motivi.append(f"n. {fattura.numero} in causale")
    pn = punteggio_nome(fattura.controparte, mov.descrizione)
    if pn:
        punti += pn
        motivi.append("controparte in causale" if pn == 25 else "controparte (parziale) in causale")
    if fattura.data_scadenza and abs((mov.data - fattura.data_scadenza).days) <= 10:
        punti += 10
        motivi.append("vicino alla scadenza")
    elif 0 <= (mov.data - fattura.data).days <= 60:
        punti += 5
    return min(punti, 100), motivi


def _fatture_candidate(mov, fatture):
    giorni_max = _cfg("GIORNI_MAX_FATTURA")
    if mov.importo > 0:
        ok = lambda f: (f.direzione == Fattura.ATTIVA) != f.nota_credito  # noqa: E731
    else:
        ok = lambda f: (f.direzione == Fattura.PASSIVA) != f.nota_credito  # noqa: E731
    return [
        f for f in fatture
        if ok(f) and f.data - timedelta(days=5) <= mov.data <= f.data + timedelta(days=giorni_max)
    ]


def proposte_fatture(mov, fatture, residui, tol):
    importo = abs(mov.importo)
    candidate = _fatture_candidate(mov, [f for f in fatture if residui[("f", f.pk)] > tol])
    proposte = []
    for f in candidate:
        residuo = residui[("f", f.pk)]
        punti, motivi = _punteggio_fattura(mov, f, residuo, tol)
        if punti < 50:
            continue
        if abs(residuo - importo) <= tol:
            quota = residuo
        elif residuo > importo and ("in causale" in " ".join(motivi)):
            quota = importo  # pagamento parziale / acconto
            motivi.append("pagamento parziale")
        else:
            continue
        proposte.append(Proposta(mov, [(f, quota, quota)], punti, motivi))

    # bonifico cumulativo: più fatture della stessa controparte
    if not any(p.punteggio >= 75 for p in proposte):
        per_controparte = {}
        for f in candidate:
            if punteggio_nome(f.controparte, mov.descrizione) or numero_in_descrizione(f.numero, mov.descrizione):
                per_controparte.setdefault(f.controparte_piva or f.controparte, []).append(f)
        for gruppo in per_controparte.values():
            gruppo = sorted(gruppo, key=lambda f: f.data)[:12]
            for n in range(2, min(4, len(gruppo)) + 1):
                for combo in itertools.combinations(gruppo, n):
                    somma = sum(residui[("f", f.pk)] for f in combo)
                    if abs(somma - importo) <= tol:
                        numeri = sum(1 for f in combo if numero_in_descrizione(f.numero, mov.descrizione))
                        punti = 60 + min(numeri * 10, 30)
                        proposte.append(
                            Proposta(
                                mov,
                                [(f, residui[("f", f.pk)], residui[("f", f.pk)]) for f in combo],
                                punti,
                                [f"somma di {n} fatture della stessa controparte"],
                            )
                        )
    return proposte


def proposte_corrispettivi(mov, corrispettivi, residui, tol):
    if mov.importo <= 0:
        return []
    importo = mov.importo
    giorni = _cfg("GIORNI_MAX_POS")
    pct = Decimal(str(_cfg("COMMISSIONE_POS_MAX_PCT"))) / 100
    descr = _testo(mov.descrizione)
    bonus_pos = 10 if any(k in descr for k in PAROLE_POS) else 0

    finestra = sorted(
        (c for c in corrispettivi
         if c.metodo != "contanti" and residui[("c", c.pk)] > tol
         and mov.data - timedelta(days=giorni) <= c.data <= mov.data),
        key=lambda c: c.data,
    )

    def compatibile(lordo):
        commissione = lordo - importo
        return -tol <= commissione <= lordo * pct + tol

    proposte = []
    # sequenze consecutive di corrispettivi (un giorno o più giorni cumulati)
    for i in range(len(finestra)):
        lordo = Decimal("0")
        for j in range(i, len(finestra)):
            lordo += residui[("c", finestra[j].pk)]
            if lordo - importo > lordo * pct + tol:
                break  # il lordo cresce con j: aggiungere giorni peggiora soltanto
            if compatibile(lordo):
                seq = finestra[i : j + 1]
                esatto = abs(lordo - importo) <= tol
                ritardo = (mov.data - seq[-1].data).days
                punti = (70 if len(seq) == 1 else 60) + (15 if esatto else 0) + bonus_pos - max(ritardo - 3, 0) * 3
                commissione = lordo - importo
                motivi = [
                    f"{len(seq)} giorn{'o' if len(seq) == 1 else 'i'} di corrispettivi elettronici",
                    "importo esatto" if esatto else f"commissione {commissione:.2f} € ({commissione / lordo * 100:.2f}%)",
                ]
                # la quota movimento si ripartisce in proporzione al lordo
                documenti, assegnato = [], Decimal("0")
                for k, c in enumerate(seq):
                    lordo_c = residui[("c", c.pk)]
                    if k == len(seq) - 1:
                        quota_mov = importo - assegnato
                    else:
                        quota_mov = (importo * lordo_c / lordo).quantize(Decimal("0.01"))
                        assegnato += quota_mov
                    documenti.append((c, quota_mov, lordo_c))
                proposte.append(Proposta(mov, documenti, max(min(punti, 100), 0), motivi))
    return proposte


def calcola_residui(azienda):
    residui = {}
    for f in Fattura.objects.filter(azienda=azienda):
        residui[("f", f.pk)] = f.residuo
    for c in Corrispettivo.objects.filter(azienda=azienda):
        residui[("c", c.pk)] = c.residuo
    return residui


@transaction.atomic
def genera_proposte(azienda, movimenti=None) -> int:
    """Ricalcola le proposte automatiche dell'azienda. Restituisce il numero di proposte create."""
    tol = Decimal(str(_cfg("TOLLERANZA_EURO")))
    Abbinamento.objects.filter(azienda=azienda, stato=Abbinamento.PROPOSTO, manuale=False).delete()

    if movimenti is None:
        movimenti = MovimentoBancario.objects.filter(azienda=azienda)
    movimenti = [m for m in movimenti if m.residuo > tol and not m.abbinamenti.filter(stato=Abbinamento.PROPOSTO).exists()]
    fatture = list(Fattura.objects.filter(azienda=azienda))
    corrispettivi = list(Corrispettivo.objects.filter(azienda=azienda))
    residui = calcola_residui(azienda)

    tutte = []
    for mov in movimenti:
        # movimenti parzialmente abbinati: si considera solo il residuo
        residuo_mov = mov.residuo
        if residuo_mov != abs(mov.importo):
            mov.importo = residuo_mov if mov.importo > 0 else -residuo_mov
        tutte += proposte_fatture(mov, fatture, residui, tol)
        tutte += proposte_corrispettivi(mov, corrispettivi, residui, tol)

    rifiutati = {
        (a.movimento_id, "f" if a.fattura_id else "c", a.fattura_id or a.corrispettivo_id)
        for a in Abbinamento.objects.filter(azienda=azienda, stato=Abbinamento.RIFIUTATO)
    }

    tutte.sort(key=lambda p: (-p.punteggio, len(p.documenti), p.movimento.data))
    movimenti_usati, creati = set(), 0
    for p in tutte:
        if p.movimento.pk in movimenti_usati:
            continue
        chiavi = [("f" if isinstance(d, Fattura) else "c", d.pk) for d, _, _ in p.documenti]
        if any((p.movimento.pk, *k) in rifiutati for k in chiavi):
            continue
        if any(residui[k] < q - tol for k, (_, _, q) in zip(chiavi, p.documenti)):
            continue  # documento già impegnato da una proposta migliore
        gruppo = str(uuid.uuid4()) if len(p.documenti) > 1 else ""
        for k, (doc, quota_mov, quota_doc) in zip(chiavi, p.documenti):
            Abbinamento.objects.create(
                azienda=azienda,
                movimento=p.movimento,
                fattura=doc if k[0] == "f" else None,
                corrispettivo=doc if k[0] == "c" else None,
                importo=quota_mov,
                importo_documento=quota_doc,
                stato=Abbinamento.PROPOSTO,
                punteggio=p.punteggio,
                motivo="; ".join(p.motivi)[:255],
                gruppo=gruppo,
            )
            residui[k] -= quota_doc
            creati += 1
        movimenti_usati.add(p.movimento.pk)
    return creati


@transaction.atomic
def conferma(abbinamento, utente=None):
    """Conferma una proposta (e tutte quelle dello stesso gruppo)."""
    qs = Abbinamento.objects.filter(azienda=abbinamento.azienda, movimento=abbinamento.movimento, stato=Abbinamento.PROPOSTO)
    if abbinamento.gruppo:
        qs = qs.filter(gruppo=abbinamento.gruppo)
    else:
        qs = qs.filter(pk=abbinamento.pk)
    return qs.update(stato=Abbinamento.CONFERMATO, creato_da=utente)


@transaction.atomic
def rifiuta(abbinamento, utente=None):
    """Scarta una proposta (con il suo gruppo): non verrà più riproposta per quel movimento."""
    qs = Abbinamento.objects.filter(azienda=abbinamento.azienda, movimento=abbinamento.movimento, stato=Abbinamento.PROPOSTO)
    qs = qs.filter(gruppo=abbinamento.gruppo) if abbinamento.gruppo else qs.filter(pk=abbinamento.pk)
    return qs.update(stato=Abbinamento.RIFIUTATO, creato_da=utente)


def conferma_sopra_soglia(azienda, utente=None, soglia=None) -> int:
    soglia = soglia if soglia is not None else _cfg("SOGLIA_AUTO_CONFERMA")
    return Abbinamento.objects.filter(azienda=azienda, stato=Abbinamento.PROPOSTO, punteggio__gte=soglia).update(
        stato=Abbinamento.CONFERMATO, creato_da=utente
    )
