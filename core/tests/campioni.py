"""Generatori di file di esempio per i test (e per provare l'app: vedi comando `crea_demo`)."""
import io
import zipfile

PIVA_AZIENDA = "01234567890"


def fattura_xml(numero, data, cedente=("Fornitore Srl", "09876543210"), cessionario=("La Mia Azienda Srl", PIVA_AZIENDA),
                imponibile="1000.00", aliquota="22.00", imposta="220.00", totale="1220.00", tipo="TD01",
                ritenuta=None, split=False, scadenza=None, importo_pagamento=None, modalita="MP05"):
    rit = f"<DatiRitenuta><TipoRitenuta>RT02</TipoRitenuta><ImportoRitenuta>{ritenuta}</ImportoRitenuta><AliquotaRitenuta>20.00</AliquotaRitenuta><CausalePagamento>A</CausalePagamento></DatiRitenuta>" if ritenuta else ""
    pag = ""
    if scadenza or importo_pagamento:
        pag = f"""<DatiPagamento><CondizioniPagamento>TP02</CondizioniPagamento><DettaglioPagamento>
          <ModalitaPagamento>{modalita}</ModalitaPagamento>
          {f'<DataScadenzaPagamento>{scadenza}</DataScadenzaPagamento>' if scadenza else ''}
          <ImportoPagamento>{importo_pagamento or totale}</ImportoPagamento>
          <IBAN>IT60X0542811101000000123456</IBAN></DettaglioPagamento></DatiPagamento>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica versione="FPR12" xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2">
  <FatturaElettronicaHeader>
    <DatiTrasmissione><IdTrasmittente><IdPaese>IT</IdPaese><IdCodice>01879020517</IdCodice></IdTrasmittente>
      <ProgressivoInvio>A1</ProgressivoInvio><FormatoTrasmissione>FPR12</FormatoTrasmissione><CodiceDestinatario>KRRH6B9</CodiceDestinatario></DatiTrasmissione>
    <CedentePrestatore><DatiAnagrafici><IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>{cedente[1]}</IdCodice></IdFiscaleIVA>
      <Anagrafica><Denominazione>{cedente[0]}</Denominazione></Anagrafica><RegimeFiscale>RF01</RegimeFiscale></DatiAnagrafici></CedentePrestatore>
    <CessionarioCommittente><DatiAnagrafici><IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>{cessionario[1]}</IdCodice></IdFiscaleIVA>
      <Anagrafica><Denominazione>{cessionario[0]}</Denominazione></Anagrafica></DatiAnagrafici></CessionarioCommittente>
  </FatturaElettronicaHeader>
  <FatturaElettronicaBody>
    <DatiGenerali><DatiGeneraliDocumento><TipoDocumento>{tipo}</TipoDocumento><Divisa>EUR</Divisa><Data>{data}</Data>
      <Numero>{numero}</Numero>{rit}<ImportoTotaleDocumento>{totale}</ImportoTotaleDocumento></DatiGeneraliDocumento></DatiGenerali>
    <DatiBeniServizi>
      <DettaglioLinee><NumeroLinea>1</NumeroLinea><Descrizione>Servizio</Descrizione><PrezzoUnitario>{imponibile}</PrezzoUnitario>
        <PrezzoTotale>{imponibile}</PrezzoTotale><AliquotaIVA>{aliquota}</AliquotaIVA></DettaglioLinee>
      <DatiRiepilogo><AliquotaIVA>{aliquota}</AliquotaIVA><ImponibileImporto>{imponibile}</ImponibileImporto><Imposta>{imposta}</Imposta>
        <EsigibilitaIVA>{'S' if split else 'I'}</EsigibilitaIVA></DatiRiepilogo>
    </DatiBeniServizi>
    {pag}
  </FatturaElettronicaBody>
</p:FatturaElettronica>""".encode()


def firma_p7m(xml: bytes) -> bytes:
    """Busta CAdES-BES (SignedData con contenuto incorporato) con certificato autofirmato."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Firma")])
    ora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome).public_key(key.public_key())
            .serial_number(1).not_valid_before(ora - timedelta(days=1)).not_valid_after(ora + timedelta(days=30))
            .sign(key, hashes.SHA256()))
    return (pkcs7.PKCS7SignatureBuilder().set_data(xml).add_signer(cert, key, hashes.SHA256())
            .sign(serialization.Encoding.DER, []))


def zip_di(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nome, dati in files.items():
            z.writestr(nome, dati)
    return buf.getvalue()


def pdf_tabella(titolo, intestazioni, righe, larghezze, allinea_destra=()):
    """PDF con una tabella disegnata come testo libero (come gli estratti conto reali)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    def intestazione_pagina():
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, h - 50, titolo)
        c.setFont("Helvetica", 8)
        c.drawString(40, h - 65, "IBAN IT00X0000000000000000000000   Saldo iniziale 1.000,00")
        y = h - 100
        c.setFont("Helvetica-Bold", 9)
        x = 40
        for i, (t, lw) in enumerate(zip(intestazioni, larghezze)):
            if i in allinea_destra:
                c.drawRightString(x + lw - 5, y, t)
            else:
                c.drawString(x, y, t)
            x += lw
        return y - 18

    y = intestazione_pagina()
    for riga in righe:
        linee = [r.split("\n") for r in riga]
        altezza = max(len(l) for l in linee)
        if y - altezza * 11 < 60:
            c.showPage()
            y = intestazione_pagina()
        c.setFont("Helvetica", 8)
        x = 40
        for i, (cella, lw) in enumerate(zip(linee, larghezze)):
            for k, testo in enumerate(cella):
                if i in allinea_destra:
                    c.drawRightString(x + lw - 5, y - k * 11, testo)
                else:
                    c.drawString(x, y - k * 11, testo)
            x += lw
        y -= altezza * 11 + 6
    c.setFont("Helvetica", 7)
    c.drawString(40, 40, "Documento generato elettronicamente - pagina 1")
    c.save()
    return buf.getvalue()


def pdf_revolut():
    righe = [
        ("2 Feb 2026", "Transfer from CLIENTE ALFA SRL\nReference: Saldo fattura 2026/15", "", "€1,220.00", "€2,220.00"),
        ("3 Feb 2026", "To Fornitore Srl\nReference: FT A-77", "€610.00", "", "€1,610.00"),
        ("4 Feb 2026", "Card payment Amazon EU", "€35.99", "", "€1,574.01"),
        ("10 Feb 2026", "Transfer from Beta Consulting\nReference: fatture 16 e 17", "", "€1,500.00", "€3,074.01"),
    ]
    return pdf_tabella("EUR Statement", ["Date", "Description", "Money out", "Money in", "Balance"], righe,
                       [70, 240, 70, 70, 70], allinea_destra=(2, 3, 4))


def pdf_sumup():
    righe = [
        ("03/02/2026", "Accredito transazioni SumUp\n02.02.2026", "+486,35", "1.486,35"),
        ("05/02/2026", "Accredito transazioni SumUp", "+688,04", "2.174,39"),
        ("06/02/2026", "Commissione mensile", "-1,50", "2.172,89"),
    ]
    return pdf_tabella("Estratto conto - SumUp Conto Aziendale", ["Data", "Descrizione", "Importo", "Saldo"], righe,
                       [80, 280, 80, 80], allinea_destra=(2, 3))


def pdf_vivid():
    # Vivid: importi con segno, senza colonna saldo, ordine decrescente
    righe = [
        ("12.02.2026", "12.02.2026", "SEPA Lastschrift Telecom Italia", "-49,90"),
        ("11.02.2026", "11.02.2026", "Gutschrift CLIENTE ALFA SRL Rechnung 2026/18", "305,00"),
    ]
    return pdf_tabella("Kontoauszug Vivid Business", ["Buchungsdatum", "Wertstellung", "Beschreibung", "Betrag"], righe,
                       [80, 80, 280, 80], allinea_destra=(3,))


CSV_REVOLUT = (
    "Date started (UTC),Date completed (UTC),ID,Type,State,Description,Reference,Payer,Card number,Orig currency,"
    "Orig amount,Payment currency,Amount,Fee,Balance,Account\n"
    "2026-02-02,2026-02-02,a1,TRANSFER,COMPLETED,Transfer from CLIENTE ALFA SRL,Saldo fattura 2026/15,,,EUR,1220,EUR,1220.00,0,2220.00,EUR Main\n"
    "2026-02-04,2026-02-04,a2,CARD_PAYMENT,COMPLETED,Amazon EU,,,,EUR,-35.99,EUR,-35.99,0,2184.01,EUR Main\n"
    "2026-02-05,,a3,CARD_PAYMENT,PENDING,Hotel,,,,EUR,-100,EUR,-100.00,0,,EUR Main\n"
)

CSV_CORRISPETTIVI_LUNGO = (
    "Data;Metodo pagamento;Importo;N. chiusura\n"
    "01/02/2026;Contanti;210,50;101\n"
    "01/02/2026;Carta;300,00;101\n"
    "02/02/2026;POS;195,00;102\n"
    "03/02/2026;Bancomat;400,00;103\n"
    "04/02/2026;Carta di credito;300,00;104\n"
)


def xlsx_corrispettivi_largo():
    from datetime import date

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Report corrispettivi febbraio"])
    ws.append([])
    ws.append(["Data", "Contanti", "Carte", "Satispay", "Totale"])
    ws.append([date(2026, 2, 1), 210.5, 300, 0, 510.5])
    ws.append([date(2026, 2, 2), 0, 195, 12, 207])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
