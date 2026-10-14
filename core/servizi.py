"""Salvataggio a database dei dati letti dai parser (con deduplica)."""
import logging
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .aruba import ArubaClient
from .models import Corrispettivo, Fattura, Importazione, MovimentoBancario
from .parsers.corrispettivi import parse_corrispettivi
from .parsers.estratti import parse_estratto
from .parsers.fatturapa import leggi_file_fatture
from .parsers.util import impronta

log = logging.getLogger(__name__)


def importa_corrispettivi(azienda, nome_file, contenuto, utente=None, metodo_default="carta"):
    righe, avvisi = parse_corrispettivi(nome_file, contenuto, metodo_default)
    imp = Importazione.objects.create(azienda=azienda, tipo="corrispettivi", nome_file=nome_file, utente=utente)
    nuovi = saltati = 0
    # righe identiche nello stesso file (stesso giorno, metodo, importo, rif.) sono distinte: le numeriamo
    occorrenze = {}
    for r in righe:
        base = (r.data, r.metodo, r.importo, r.riferimento)
        occorrenze[base] = occorrenze.get(base, 0) + 1
        chiave = impronta(*base, occorrenze[base])
        _, creato = Corrispettivo.objects.get_or_create(
            azienda=azienda,
            impronta=chiave,
            defaults=dict(importazione=imp, data=r.data, metodo=r.metodo, importo=r.importo,
                          riferimento=r.riferimento[:100], descrizione=r.descrizione[:255]),
        )
        nuovi += creato
        saltati += not creato
    imp.righe_importate, imp.righe_saltate = nuovi, saltati
    imp.note = "\n".join(avvisi[:200])
    imp.save()
    return imp


def importa_estratto(azienda, conto, nome_file, contenuto, utente=None):
    movimenti, avvisi = parse_estratto(nome_file, contenuto, conto.banca)
    imp = Importazione.objects.create(azienda=azienda, tipo="estratto", nome_file=nome_file, utente=utente)
    nuovi = saltati = 0
    occorrenze = {}
    for m in movimenti:
        base = (m.data, m.importo, m.descrizione)
        occorrenze[base] = occorrenze.get(base, 0) + 1
        # il saldo distingue movimenti identici nello stesso giorno; se manca si usa l'ordinale
        chiave = impronta(*base, m.saldo if m.saldo is not None else f"#{occorrenze[base]}")
        _, creato = MovimentoBancario.objects.get_or_create(
            conto=conto,
            impronta=chiave,
            defaults=dict(azienda=azienda, importazione=imp, data=m.data, data_valuta=m.data_valuta,
                          descrizione=m.descrizione, importo=m.importo, saldo=m.saldo),
        )
        nuovi += creato
        saltati += not creato
    if not movimenti:
        avvisi.insert(0, "Nessun movimento riconosciuto nel file: verificare il formato o usare l'export CSV della banca.")
    imp.righe_importate, imp.righe_saltate = nuovi, saltati
    imp.note = "\n".join(avvisi[:200])
    imp.save()
    return imp


def _salva_fattura(azienda, dati, imp, direzione_suggerita=None, id_sdi=""):
    direzione = dati.direzione_per(azienda.identificativi(), direzione_suggerita)
    if direzione is None:
        raise ValueError(
            f"{dati.nome_file}: l'azienda (P.IVA {azienda.partita_iva or '-'}) non è né cedente né cessionario"
        )
    if direzione == Fattura.ATTIVA:
        nome, piva = dati.cessionario_nome, dati.cessionario_piva or dati.cessionario_cf
    else:
        nome, piva = dati.cedente_nome, dati.cedente_piva or dati.cedente_cf
    try:
        with transaction.atomic():
            _, creata = Fattura.objects.update_or_create(
                azienda=azienda, direzione=direzione, controparte_piva=piva, numero=dati.numero, data=dati.data,
                defaults=dict(
                    importazione=imp, tipo_documento=dati.tipo_documento, controparte=nome[:255],
                    imponibile=dati.imponibile, imposta=dati.imposta, totale=dati.totale,
                    netto_a_pagare=dati.netto_a_pagare, data_scadenza=dati.data_scadenza,
                    modalita_pagamento=dati.modalita_pagamento, iban_pagamento=dati.iban_pagamento,
                    nome_file_sdi=dati.nome_file[:255], id_sdi=str(id_sdi or "")[:50],
                ),
            )
    except IntegrityError as exc:
        raise ValueError(f"{dati.nome_file}: {exc}") from exc
    return creata


def importa_fatture_file(azienda, nome_file, contenuto, utente=None, direzione_suggerita=None):
    fatture, errori = leggi_file_fatture(nome_file, contenuto)
    imp = Importazione.objects.create(azienda=azienda, tipo="fatture_file", nome_file=nome_file, utente=utente)
    nuove = aggiornate = 0
    for f in fatture:
        try:
            if _salva_fattura(azienda, f, imp, direzione_suggerita):
                nuove += 1
            else:
                aggiornate += 1
        except ValueError as exc:
            errori.append(str(exc))
    imp.righe_importate, imp.righe_saltate = nuove, aggiornate
    imp.note = "\n".join(errori[:200])
    imp.save()
    return imp


def sincronizza_aruba(azienda, dal: date | None = None, al: date | None = None, utente=None, client=None):
    """Scarica da Aruba le fatture emesse e ricevute nel periodo e le salva."""
    cred = azienda.aruba
    al = al or timezone.localdate()
    if dal is None:
        dal = (cred.ultima_sincronizzazione.date() - timedelta(days=7)) if cred.ultima_sincronizzazione else al - timedelta(days=365)
    client = client or ArubaClient(cred.username, cred.password, cred.ambiente)
    imp = Importazione.objects.create(azienda=azienda, tipo="fatture_aruba", nome_file=f"{dal} → {al}", utente=utente)
    nuove = aggiornate = 0
    errori = []
    gia_presenti = set(Fattura.objects.filter(azienda=azienda).exclude(nome_file_sdi="").values_list("nome_file_sdi", flat=True))
    for direzione_api, direzione in (("out", Fattura.ATTIVA), ("in", Fattura.PASSIVA)):
        for rec in client.elenco(direzione_api, dal, al):
            filename = rec.get("filename") or rec.get("fileName")
            if not filename or filename in gia_presenti:
                continue
            try:
                contenuto = client.scarica_file(direzione_api, filename)
                nome = filename if filename.lower().endswith((".xml", ".p7m")) else filename + ".xml"
                fatture, err = leggi_file_fatture(nome, contenuto)
                errori += err
                for f in fatture:
                    f.nome_file = filename
                    if _salva_fattura(azienda, f, imp, direzione, rec.get("idSdi", "")):
                        nuove += 1
                    else:
                        aggiornate += 1
            except Exception as exc:
                log.exception("Errore su fattura Aruba %s", filename)
                errori.append(f"{filename}: {exc}")
    cred.ultima_sincronizzazione = timezone.now()
    cred.save(update_fields=["ultima_sincronizzazione"])
    imp.righe_importate, imp.righe_saltate = nuove, aggiornate
    imp.note = "\n".join(errori[:200])
    imp.save()
    return imp
