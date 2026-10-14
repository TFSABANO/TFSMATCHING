"""Lettura fatture elettroniche FatturaPA (XML / XML.p7m / ZIP di fatture)."""
import base64
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from lxml import etree

from ..models import normalizza_id_fiscale
from .util import parse_data, parse_importo

ZERO = Decimal("0.00")


@dataclass
class DatiFattura:
    tipo_documento: str
    numero: str
    data: date
    cedente_nome: str
    cedente_piva: str
    cedente_cf: str
    cessionario_nome: str
    cessionario_piva: str
    cessionario_cf: str
    imponibile: Decimal
    imposta: Decimal
    totale: Decimal
    netto_a_pagare: Decimal
    data_scadenza: date | None = None
    modalita_pagamento: str = ""
    iban_pagamento: str = ""
    nome_file: str = ""
    avvisi: list = field(default_factory=list)

    def direzione_per(self, ids_azienda: set[str], suggerita: str | None = None) -> str | None:
        """'attiva' se l'azienda è il cedente, 'passiva' se è il cessionario."""
        ced = {normalizza_id_fiscale(self.cedente_piva), normalizza_id_fiscale(self.cedente_cf)} - {""}
        ces = {normalizza_id_fiscale(self.cessionario_piva), normalizza_id_fiscale(self.cessionario_cf)} - {""}
        if ids_azienda & ced:
            return "attiva"
        if ids_azienda & ces:
            return "passiva"
        return suggerita


def estrai_xml_da_p7m(dati: bytes) -> bytes:
    """Estrae l'XML firmato da un file CAdES (.p7m), anche se codificato in base64."""
    grezzi = dati
    if not grezzi.lstrip().startswith(b"0") and not grezzi.startswith(b"\x30"):
        try:
            grezzi = base64.b64decode(re.sub(rb"\s+", b"", dati), validate=False)
        except Exception:
            grezzi = dati
    try:
        from asn1crypto import cms

        info = cms.ContentInfo.load(grezzi)
        contenuto = info["content"]["encap_content_info"]["content"].native
        if contenuto:
            return contenuto
    except Exception:
        pass
    # fallback robusto: cerca il blocco XML nel binario
    m = re.search(rb"<\?xml.*?</(?:\w+:)?FatturaElettronica>", grezzi, re.S)
    if not m:
        m = re.search(rb"<(?:\w+:)?FatturaElettronica[\s>].*?</(?:\w+:)?FatturaElettronica>", grezzi, re.S)
    if not m:
        raise ValueError("Impossibile estrarre l'XML dal file p7m")
    return m.group(0)


def _testo(el, percorso):
    """Cerca un percorso 'A/B/C' ignorando i namespace; restituisce il testo o ''."""
    parti = percorso.split("/")
    xp = "./" + "/".join(f"*[local-name()='{p}']" for p in parti)
    trovati = el.xpath(xp)
    return (trovati[0].text or "").strip() if trovati else ""


def _tutti(el, percorso):
    parti = percorso.split("/")
    xp = "./" + "/".join(f"*[local-name()='{p}']" for p in parti)
    return el.xpath(xp)


def _anagrafica(soggetto):
    ana = "DatiAnagrafici/Anagrafica/"
    nome = _testo(soggetto, ana + "Denominazione")
    if not nome:
        nome = f"{_testo(soggetto, ana + 'Nome')} {_testo(soggetto, ana + 'Cognome')}".strip()
    piva = _testo(soggetto, "DatiAnagrafici/IdFiscaleIVA/IdCodice")
    cf = _testo(soggetto, "DatiAnagrafici/CodiceFiscale")
    return nome, piva, cf


def parse_fattura_xml(xml: bytes, nome_file: str = "") -> list[DatiFattura]:
    """Una FatturaPA può contenere più FatturaElettronicaBody (lotto): restituisce una voce per body."""
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True)
    root = etree.fromstring(xml.strip(), parser)
    if root is None:
        raise ValueError("XML non valido")
    header = _tutti(root, "FatturaElettronicaHeader")
    if not header:
        raise ValueError("Non è una FatturaPA (manca FatturaElettronicaHeader)")
    header = header[0]
    ced = _anagrafica(_tutti(header, "CedentePrestatore")[0])
    ces_el = _tutti(header, "CessionarioCommittente")
    ces = _anagrafica(ces_el[0]) if ces_el else ("", "", "")

    risultati = []
    for body in _tutti(root, "FatturaElettronicaBody"):
        dgd = "DatiGenerali/DatiGeneraliDocumento/"
        avvisi = []
        imponibile = sum((parse_importo(e.text) or ZERO for e in _tutti(body, "DatiBeniServizi/DatiRiepilogo/ImponibileImporto")), ZERO)
        imposta = sum((parse_importo(e.text) or ZERO for e in _tutti(body, "DatiBeniServizi/DatiRiepilogo/Imposta")), ZERO)
        split_payment = any((e.text or "").strip() == "S" for e in _tutti(body, "DatiBeniServizi/DatiRiepilogo/EsigibilitaIVA"))

        totale = parse_importo(_testo(body, dgd + "ImportoTotaleDocumento"))
        if totale is None:
            totale = imponibile + imposta
            avvisi.append("ImportoTotaleDocumento assente: calcolato da riepilogo IVA")
        ritenute = sum((parse_importo(e.text) or ZERO for e in _tutti(body, dgd + "DatiRitenuta/ImportoRitenuta")), ZERO)

        dettagli = _tutti(body, "DatiPagamento/DettaglioPagamento")
        importi_pag = [parse_importo(_testo(d, "ImportoPagamento")) for d in dettagli]
        importi_pag = [i for i in importi_pag if i is not None]
        if importi_pag:
            netto = sum(importi_pag, ZERO)
        else:
            netto = totale - ritenute - (imposta if split_payment else ZERO)

        scadenze = [parse_data(_testo(d, "DataScadenzaPagamento")) for d in dettagli]
        scadenze = [s for s in scadenze if s]
        modalita = _testo(dettagli[0], "ModalitaPagamento") if dettagli else ""
        iban = next((_testo(d, "IBAN") for d in dettagli if _testo(d, "IBAN")), "")

        d = parse_data(_testo(body, dgd + "Data"))
        if not d:
            raise ValueError(f"{nome_file}: data documento mancante")
        risultati.append(
            DatiFattura(
                tipo_documento=_testo(body, dgd + "TipoDocumento") or "TD01",
                numero=_testo(body, dgd + "Numero"),
                data=d,
                cedente_nome=ced[0], cedente_piva=ced[1], cedente_cf=ced[2],
                cessionario_nome=ces[0], cessionario_piva=ces[1], cessionario_cf=ces[2],
                imponibile=imponibile,
                imposta=imposta,
                totale=totale,
                netto_a_pagare=netto,
                data_scadenza=max(scadenze) if scadenze else None,
                modalita_pagamento=modalita,
                iban_pagamento=iban,
                nome_file=nome_file,
                avvisi=avvisi,
            )
        )
    return risultati


def _e_metadati(nome: str) -> bool:
    # file di servizio SdI: metadati (_MT_), ricevute (_RC_, _NS_, _MC_, _NE_, _DT_, _AT_, _EC_)
    return bool(re.search(r"_(MT|RC|NS|MC|NE|DT|AT|EC|SE)_\d+", nome, re.I))


def leggi_file_fatture(nome_file: str, contenuto: bytes, _profondita=0) -> tuple[list[DatiFattura], list[str]]:
    """Accetta .xml, .p7m, .zip (anche annidati). Restituisce fatture ed eventuali errori per file."""
    fatture, errori = [], []
    nome = nome_file.lower()
    try:
        if nome.endswith(".zip"):
            if _profondita > 3:
                return [], [f"{nome_file}: troppi livelli di ZIP annidati"]
            with zipfile.ZipFile(io.BytesIO(contenuto)) as z:
                for info in z.infolist():
                    if info.is_dir() or info.file_size > 50 * 1024 * 1024:
                        continue
                    base = info.filename.rsplit("/", 1)[-1]
                    if base.startswith(".") or _e_metadati(base):
                        continue
                    if base.lower().endswith((".xml", ".p7m", ".zip")):
                        f, e = leggi_file_fatture(base, z.read(info), _profondita + 1)
                        fatture += f
                        errori += e
        elif nome.endswith(".p7m"):
            fatture = parse_fattura_xml(estrai_xml_da_p7m(contenuto), nome_file)
        elif nome.endswith(".xml"):
            if _e_metadati(nome_file):
                return [], []
            fatture = parse_fattura_xml(contenuto, nome_file)
        else:
            errori.append(f"{nome_file}: estensione non supportata")
    except Exception as exc:  # un file rovinato non deve bloccare l'intero import
        errori.append(f"{nome_file}: {exc}")
    return fatture, errori
