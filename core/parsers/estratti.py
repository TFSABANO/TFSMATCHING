"""Lettura estratti conto (PDF o CSV/Excel) di Revolut Business, Vivid, SumUp e generici.

Approccio per i PDF (indipendente dal layout preciso della banca):

1. si estraggono le parole con la loro posizione (pdfplumber) e si raggruppano in righe;
2. si cerca la riga di intestazione della tabella movimenti (deve contenere una colonna data e
   almeno una colonna importo: "Money out/in", "Uscite/Entrate", "Importo", "Betrag", ...);
3. ogni riga che inizia con una data apre un movimento; le righe successive senza data
   sono il proseguimento della descrizione;
4. ogni importo viene assegnato alla colonna di intestazione più vicina in orizzontale
   (uscite, entrate, importo con segno, saldo);
5. il segno viene verificato con il saldo progressivo quando disponibile.

I profili per banca aggiungono solo parole chiave; se un estratto non viene letto bene si
può intervenire qui senza toccare il resto dell'applicazione.
"""
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .corrispettivi import _chiave, leggi_tabella
from .util import normalizza_testo, parse_data, parse_importo

RE_IMPORTO = re.compile(r"^[-+−]?\(?[-+−]?(?:€|EUR)?\s?[-+−]?\d{1,3}(?:[.,' ]?\d{3})*[.,]\d{2}\)?-?(?:€|EUR)?$")

COLONNE_BASE = {
    "data": ["date", "data", "datum", "data operazione", "data contabile", "booking date", "buchungsdatum",
             "transaction date", "completed date", "date completed", "buchungstag"],
    "valuta": ["value date", "data valuta", "valuta", "wertstellung", "valutadatum"],
    "descrizione": ["description", "descrizione", "beschreibung", "verwendungszweck", "details", "causale",
                    "transaction", "operazione", "dettagli", "reference", "riferimento"],
    "uscite": ["money out", "uscite", "addebiti", "addebito", "dare", "debit", "debits", "soll", "ausgang",
               "paid out", "withdrawals"],
    "entrate": ["money in", "entrate", "accrediti", "accredito", "avere", "credit", "credits", "haben", "eingang",
                "paid in", "deposits"],
    "importo": ["amount", "importo", "betrag", "amount eur", "importo eur", "betrag eur"],
    "saldo": ["balance", "saldo", "kontostand", "running balance"],
}

PROFILI = {
    "revolut": {"salta_sezioni": ["reverted", "pending", "stornate", "in sospeso"]},
    "vivid": {"salta_sezioni": []},
    "sumup": {"salta_sezioni": []},
    "generico": {"salta_sezioni": []},
}

# piè di pagina / note legali: non sono mai proseguimento di una descrizione
RIGHE_PIE_PAGINA = re.compile(
    r"(\bpag(ina|e)?\.?\s*\d+(\s*(di|of|von|/)\s*\d+)?\s*$|seite \d+|generat[oe]d? (il|on|elettronicamente)|"
    r"documento generato|revolut (bank|ltd)|sumup (payments|limited|bank)|vivid money|regulated by|autorizzat[ao] da)",
    re.I,
)
# distanza verticale massima (punti PDF) tra due righe della stessa descrizione
MAX_SALTO_RIGA = 24

RIGHE_DA_IGNORARE = re.compile(
    r"(saldo (iniziale|finale|contabile|disponibile)|opening balance|closing balance|balance brought forward|"
    r"balance carried forward|anfangssaldo|endsaldo|alter kontostand|neuer kontostand|^totale?\b|^total\b|"
    r"^summe\b|riporto)",
    re.I,
)


@dataclass
class MovimentoGrezzo:
    data: date
    descrizione: str
    importo: Decimal
    saldo: Decimal | None = None
    data_valuta: date | None = None


@dataclass
class _Parola:
    testo: str
    x0: float
    x1: float
    top: float = 0.0

    @property
    def xc(self):
        return (self.x0 + self.x1) / 2


# --------------------------------------------------------------------------- PDF


def _righe_pdf(contenuto: bytes):
    import pdfplumber

    with pdfplumber.open(io.BytesIO(contenuto)) as pdf:
        for pagina in pdf.pages:
            parole = pagina.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
            parole.sort(key=lambda w: (round(w["top"]), w["x0"]))
            riga, top_corrente = [], None
            for w in parole:
                if top_corrente is not None and abs(w["top"] - top_corrente) > 3:
                    yield sorted(riga, key=lambda p: p.x0)
                    riga = []
                if not riga:
                    top_corrente = w["top"]
                riga.append(_Parola(w["text"], w["x0"], w["x1"], w["top"]))
            if riga:
                yield sorted(riga, key=lambda p: p.x0)
            yield None  # separatore di pagina


def _riconosci_intestazione(parole: list[_Parola]):
    """Se la riga è un'intestazione di tabella restituisce {tipo_colonna: (x0, x1)}."""
    colonne = {}
    i = 0
    while i < len(parole):
        trovato = False
        for n in (3, 2, 1):  # prima le frasi più lunghe ("data valuta", "money out")
            if i + n > len(parole):
                continue
            frase = _chiave(" ".join(p.testo for p in parole[i : i + n]))
            for tipo, sinonimi in COLONNE_BASE.items():
                if frase in sinonimi and tipo not in colonne:
                    colonne[tipo] = (parole[i].x0, parole[i + n - 1].x1)
                    i += n
                    trovato = True
                    break
            if trovato:
                break
        if not trovato:
            i += 1
    ha_importi = any(t in colonne for t in ("uscite", "entrate", "importo"))
    if "data" in colonne and ha_importi:
        return colonne
    return None


def _data_iniziale(parole):
    """Prova a leggere una data dalle prime 1-3 parole; restituisce (data, n_parole)."""
    if not parole:
        return None, 0
    if re.match(r"^\d{1,4}[/.\-]\d", parole[0].testo):  # 03/01/2026, 2026-01-03, 03.01.2026
        d = parse_data(parole[0].testo)
        return (d, 1) if d else (None, 0)
    if len(parole) >= 3:  # "3 Jan 2026", "03 gen 2026", "Jan 3, 2026"
        d = parse_data(" ".join(p.testo for p in parole[:3]))
        if d:
            return d, 3
    return None, 0


def _unisci_segni(parole):
    """Unisce token separati come '-' '12,00' oppure '12,00' '€'."""
    out = []
    for p in parole:
        if out and out[-1].testo in ("-", "+", "−", "€") and RE_IMPORTO.match(p.testo) and p.x0 - out[-1].x1 < 6:
            prev = out.pop()
            segno = "-" if prev.testo in ("-", "−") else ""
            out.append(_Parola(segno + p.testo, prev.x0, p.x1, p.top))
        elif p.testo in ("€", "EUR") and out and RE_IMPORTO.match(out[-1].testo):
            out[-1] = _Parola(out[-1].testo, out[-1].x0, p.x1, p.top)
        else:
            out.append(p)
    return out


def _colonna_piu_vicina(parola, colonne, tipi):
    candidati = [(t, colonne[t]) for t in tipi if t in colonne]
    if not candidati:
        return None

    def distanza(c):
        x0, x1 = c[1]
        # gli importi sono di solito allineati a destra: confronta sia il centro sia il bordo destro
        return min(abs(parola.xc - (x0 + x1) / 2), abs(parola.x1 - x1))

    return min(candidati, key=distanza)[0]


def parse_estratto_pdf(contenuto: bytes, banca: str = "generico") -> tuple[list[MovimentoGrezzo], list[str]]:
    profilo = PROFILI.get(banca, PROFILI["generico"])
    movimenti, avvisi = [], []
    colonne = None
    corrente = None
    in_sezione_saltata = False

    def chiudi():
        nonlocal corrente
        if corrente and corrente.get("importo") is not None:
            movimenti.append(
                MovimentoGrezzo(
                    data=corrente["data"],
                    descrizione=normalizza_testo(" ".join(corrente["descr"])),
                    importo=corrente["importo"],
                    saldo=corrente.get("saldo"),
                    data_valuta=corrente.get("valuta"),
                )
            )
        elif corrente:
            avvisi.append(f"Movimento del {corrente['data']} senza importo riconosciuto: {' '.join(corrente['descr'])[:80]}")
        corrente = None

    for parole in _righe_pdf(contenuto):
        if parole is None:  # fine pagina: le righe in testa alla pagina successiva (titoli, logo) non sono descrizione
            chiudi()
            continue
        testo_riga = " ".join(p.testo for p in parole)
        chiave_riga = _chiave(testo_riga)

        intest = _riconosci_intestazione(parole)
        if intest:
            chiudi()
            colonne = intest
            in_sezione_saltata = False
            continue
        if any(chiave_riga.startswith(s) for s in profilo["salta_sezioni"]):
            chiudi()
            in_sezione_saltata = True
            continue
        if colonne is None or in_sezione_saltata:
            continue

        parole = _unisci_segni(parole)
        d, n = _data_iniziale(parole)
        if d and "descrizione" in colonne and parole[0].x0 >= colonne["descrizione"][0] - 2:
            d, n = None, 0  # data scritta nella colonna descrizione: è testo, non un nuovo movimento
        resto = parole[n:] if d else parole

        if d and RIGHE_DA_IGNORARE.search(" ".join(p.testo for p in resto)):
            chiudi()
            continue

        if d:
            chiudi()
            corrente = {"data": d, "descr": [], "importo": None, "ultimo_top": parole[0].top}
            # eventuale data valuta subito dopo
            if resto and "valuta" in colonne:
                dv = parse_data(resto[0].testo)
                if dv and _colonna_piu_vicina(resto[0], colonne, ["data", "valuta", "descrizione"]) == "valuta":
                    corrente["valuta"] = dv
                    resto = resto[1:]
        elif corrente is None:
            continue
        elif RIGHE_DA_IGNORARE.search(testo_riga):
            chiudi()
            continue
        elif RIGHE_PIE_PAGINA.search(testo_riga) or parole[0].top - corrente["ultimo_top"] > MAX_SALTO_RIGA:
            chiudi()  # riga staccata dal movimento (piè di pagina, note): non fa parte della descrizione
            continue
        if corrente is not None:
            corrente["ultimo_top"] = parole[0].top

        for p in resto:
            if RE_IMPORTO.match(p.testo):
                tipo = _colonna_piu_vicina(p, colonne, ["uscite", "entrate", "importo", "saldo"])
                # un importo sotto la colonna descrizione è parte del testo (es. "Fattura 1.234,00")
                if "descrizione" in colonne:
                    dx0, dx1 = colonne["descrizione"]
                    prossima_num = min(
                        (colonne[t][0] for t in ("uscite", "entrate", "importo", "saldo") if t in colonne), default=1e9
                    )
                    if p.x1 < prossima_num - 5 and p.x0 >= dx0 - 2:
                        corrente["descr"].append(p.testo)
                        continue
                valore = parse_importo(p.testo)
                if tipo == "saldo":
                    corrente["saldo"] = valore
                elif corrente["importo"] is None:
                    if tipo == "uscite":
                        corrente["importo"] = -abs(valore)
                    elif tipo == "entrate":
                        corrente["importo"] = abs(valore)
                    else:
                        corrente["importo"] = valore
                        corrente["segno_esplicito"] = p.testo.lstrip("(€ ").startswith(("-", "+", "−"))
                else:
                    corrente["descr"].append(p.testo)
            else:
                corrente["descr"].append(p.testo)
    chiudi()

    correggi_segni_con_saldo(movimenti)
    return movimenti, avvisi


def correggi_segni_con_saldo(movimenti: list[MovimentoGrezzo]):
    """Se i saldi progressivi sono disponibili, usa la differenza per correggere segni dubbi.

    Funziona sia con ordine cronologico crescente sia decrescente.
    """
    con_saldo = [m for m in movimenti if m.saldo is not None]
    if len(con_saldo) < 2:
        return
    tol = Decimal("0.01")
    for direzione in (1, -1):
        seq = movimenti if direzione == 1 else list(reversed(movimenti))
        coerenti = 0
        for prev, cur in zip(seq, seq[1:]):
            if prev.saldo is None or cur.saldo is None:
                continue
            if abs(prev.saldo + cur.importo - cur.saldo) <= tol:
                coerenti += 1
            elif abs(prev.saldo - cur.importo - cur.saldo) <= tol:
                cur.importo = -cur.importo
                coerenti += 1
        if coerenti >= (len(con_saldo) - 1) * 0.6:
            return


# --------------------------------------------------------------------------- CSV / Excel

CSV_DATA = ["date completed (utc)", "date completed", "completed date", "data", "date", "booking date",
            "buchungsdatum", "data operazione", "data contabile", "transaction date", "date started (utc)"]
CSV_VALUTA = ["data valuta", "value date", "valuta", "wertstellung"]
CSV_DESCR = ["description", "descrizione", "beschreibung", "verwendungszweck", "causale", "details", "dettagli"]
CSV_EXTRA = ["reference", "riferimento", "payer", "counterparty", "controparte", "beneficiario", "ordinante",
             "partner", "name", "empfanger", "auftraggeber"]
CSV_IMPORTO = ["amount", "importo", "betrag", "total amount", "importo eur", "amount (eur)"]
CSV_USCITE = ["money out", "uscite", "addebiti", "dare", "debit", "soll", "paid out"]
CSV_ENTRATE = ["money in", "entrate", "accrediti", "avere", "credit", "haben", "paid in"]
CSV_SALDO = ["balance", "saldo", "kontostand"]
CSV_STATO = ["state", "stato", "status"]
CSV_COMMISSIONE = ["fee", "commissione", "fees", "gebuhr"]


def parse_estratto_tabella(nome_file: str, contenuto: bytes) -> tuple[list[MovimentoGrezzo], list[str]]:
    righe = leggi_tabella(nome_file, contenuto)
    idx, chiavi = None, None
    norm = lambda lista: {_chiave(s) for s in lista}  # noqa: E731
    for i, r in enumerate(righe[:30]):
        k = [_chiave(c) for c in r]
        if any(c in norm(CSV_DATA) for c in k) and any(c in norm(CSV_IMPORTO + CSV_USCITE + CSV_ENTRATE) for c in k):
            idx, chiavi = i, k
            break
    if idx is None:
        raise ValueError("Intestazione non trovata: servono almeno una colonna data e una colonna importo")

    def col(sinonimi):
        for s in sinonimi:
            if _chiave(s) in chiavi:
                return chiavi.index(_chiave(s))
        return None

    i_data, i_val, i_des, i_imp = col(CSV_DATA), col(CSV_VALUTA), col(CSV_DESCR), col(CSV_IMPORTO)
    i_out, i_in, i_sal, i_stato, i_fee = col(CSV_USCITE), col(CSV_ENTRATE), col(CSV_SALDO), col(CSV_STATO), col(CSV_COMMISSIONE)
    i_extra = [chiavi.index(_chiave(s)) for s in CSV_EXTRA if _chiave(s) in chiavi]

    def cella(r, i):
        return r[i] if i is not None and i < len(r) else None

    movimenti, avvisi = [], []
    for n, r in enumerate(righe[idx + 1 :], start=idx + 2):
        if not r or all(c in (None, "") for c in r):
            continue
        stato = _chiave(cella(r, i_stato) or "")
        if stato and stato not in ("completed", "completato", "completata", "eseguito", "booked", "gebucht", "successful"):
            continue  # Revolut: PENDING / REVERTED / DECLINED non sono movimenti contabilizzati
        d = parse_data(cella(r, i_data))
        if not d:
            avvisi.append(f"Riga {n}: data non valida")
            continue
        if i_imp is not None:
            imp = parse_importo(cella(r, i_imp))
        else:
            uscita = parse_importo(cella(r, i_out)) or Decimal("0")
            entrata = parse_importo(cella(r, i_in)) or Decimal("0")
            imp = entrata - abs(uscita)
        if not imp:
            continue
        fee = parse_importo(cella(r, i_fee))
        if fee and i_imp is not None:
            imp = imp - abs(fee)  # Revolut esporta la commissione separata dall'importo
        descr = " - ".join(str(cella(r, i)).strip() for i in [i_des, *i_extra] if cella(r, i) not in (None, ""))
        movimenti.append(
            MovimentoGrezzo(d, normalizza_testo(descr), imp, parse_importo(cella(r, i_sal)), parse_data(cella(r, i_val)))
        )
    return movimenti, avvisi


def parse_estratto(nome_file: str, contenuto: bytes, banca: str = "generico"):
    if nome_file.lower().endswith(".pdf"):
        return parse_estratto_pdf(contenuto, banca)
    return parse_estratto_tabella(nome_file, contenuto)
