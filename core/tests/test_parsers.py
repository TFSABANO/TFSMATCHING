from datetime import date
from decimal import Decimal as D

from django.test import SimpleTestCase

from core.parsers.corrispettivi import parse_corrispettivi
from core.parsers.estratti import parse_estratto
from core.parsers.fatturapa import leggi_file_fatture
from core.parsers.util import parse_data, parse_importo

from . import campioni


class UtilTest(SimpleTestCase):
    def test_importi(self):
        casi = {
            "1.234,56": D("1234.56"), "1,234.56": D("1234.56"), "-12,5": D("-12.50"), "€1,220.00": D("1220.00"),
            "(12.50)": D("-12.50"), "+486,35": D("486.35"), "1.000": D("1000.00"), "12,00 €": D("12.00"),
            "1 234,56": D("1234.56"), "35.99": D("35.99"), "100-": D("-100.00"), "abc": None, "": None,
        }
        for testo, atteso in casi.items():
            self.assertEqual(parse_importo(testo), atteso, testo)

    def test_date(self):
        self.assertEqual(parse_data("03/02/2026"), date(2026, 2, 3))
        self.assertEqual(parse_data("03.02.26"), date(2026, 2, 3))
        self.assertEqual(parse_data("2026-02-03"), date(2026, 2, 3))
        self.assertEqual(parse_data("3 Feb 2026"), date(2026, 2, 3))
        self.assertEqual(parse_data("3 feb 2026"), date(2026, 2, 3))
        self.assertEqual(parse_data("3 Okt 2026"), date(2026, 10, 3))
        self.assertEqual(parse_data("Feb 3, 2026"), date(2026, 2, 3))
        self.assertIsNone(parse_data("31/02/2026"))


class FatturaPATest(SimpleTestCase):
    def test_xml_semplice(self):
        xml = campioni.fattura_xml("A-77", "2026-01-20", scadenza="2026-02-19")
        [f], err = leggi_file_fatture("IT09876543210_00001.xml", xml)
        self.assertEqual(err, [])
        self.assertEqual(f.numero, "A-77")
        self.assertEqual(f.totale, D("1220.00"))
        self.assertEqual(f.netto_a_pagare, D("1220.00"))
        self.assertEqual(f.data_scadenza, date(2026, 2, 19))
        self.assertEqual(f.direzione_per({campioni.PIVA_AZIENDA}), "passiva")

    def test_ritenuta_e_split(self):
        xml = campioni.fattura_xml("5", "2026-01-10", ritenuta="200.00")
        [f], _ = leggi_file_fatture("a.xml", xml)
        self.assertEqual(f.netto_a_pagare, D("1020.00"))
        xml = campioni.fattura_xml("6", "2026-01-10", split=True)
        [f], _ = leggi_file_fatture("a.xml", xml)
        self.assertEqual(f.netto_a_pagare, D("1000.00"))

    def test_p7m_e_zip(self):
        xml = campioni.fattura_xml("2026/15", "2026-01-15", cedente=("La Mia Azienda Srl", campioni.PIVA_AZIENDA),
                                   cessionario=("Cliente Alfa Srl", "11111111111"))
        p7m = campioni.firma_p7m(xml)
        z = campioni.zip_di({
            "IT01234567890_0001.xml.p7m": p7m,
            "IT01234567890_0001_MT_001.xml": b"<metadati/>",
            "annidato.zip": campioni.zip_di({"b.xml": campioni.fattura_xml("9", "2026-01-01")}),
            "rotto.xml": b"<non valido",
        })
        fatture, err = leggi_file_fatture("export.zip", z)
        self.assertEqual(sorted(f.numero for f in fatture), ["2026/15", "9"])
        self.assertEqual(len(err), 1)
        self.assertIn("rotto.xml", err[0])
        f = next(f for f in fatture if f.numero == "2026/15")
        self.assertEqual(f.direzione_per({campioni.PIVA_AZIENDA}), "attiva")
        self.assertEqual(f.cessionario_nome, "Cliente Alfa Srl")


class CorrispettiviTest(SimpleTestCase):
    def test_csv_lungo(self):
        righe, avvisi = parse_corrispettivi("c.csv", campioni.CSV_CORRISPETTIVI_LUNGO.encode())
        self.assertEqual(len(righe), 5)
        self.assertEqual([r.metodo for r in righe], ["contanti", "carta", "carta", "carta", "carta"])
        self.assertEqual(righe[0].importo, D("210.50"))
        self.assertEqual(righe[0].riferimento, "101")

    def test_xlsx_largo(self):
        righe, _ = parse_corrispettivi("c.xlsx", campioni.xlsx_corrispettivi_largo())
        self.assertEqual(
            [(r.data.day, r.metodo, r.importo) for r in righe],
            [(1, "contanti", D("210.50")), (1, "carta", D("300.00")), (2, "carta", D("195.00")), (2, "altro", D("12.00"))],
        )


class EstrattiTest(SimpleTestCase):
    def test_revolut_pdf(self):
        movimenti, avvisi = parse_estratto("rev.pdf", campioni.pdf_revolut(), "revolut")
        self.assertEqual(avvisi, [])
        self.assertEqual([m.importo for m in movimenti], [D("1220.00"), D("-610.00"), D("-35.99"), D("1500.00")])
        self.assertEqual(movimenti[0].data, date(2026, 2, 2))
        self.assertIn("Saldo fattura 2026/15", movimenti[0].descrizione)
        self.assertEqual(movimenti[3].saldo, D("3074.01"))
        self.assertEqual(movimenti[3].descrizione, "Transfer from Beta Consulting Reference: fatture 16 e 17")

    def test_pdf_piu_pagine(self):
        righe, saldo = [], D("1000.00")
        for i in range(1, 121):
            imp = D(i) if i % 3 else -D(i)
            saldo += imp
            righe.append((f"{(i % 28) + 1:02d}/03/2026", f"Movimento {i}\nriga due {i}", f"{imp:+.2f}".replace(".", ","),
                          f"{saldo:.2f}".replace(".", ",")))
        pdf = campioni.pdf_tabella("Estratto", ["Data", "Descrizione", "Importo", "Saldo"], righe, [80, 280, 80, 80], (2, 3))
        movimenti, avvisi = parse_estratto("x.pdf", pdf, "sumup")
        self.assertEqual(len(movimenti), 120)
        self.assertEqual(sum(m.importo for m in movimenti), saldo - D("1000.00"))
        self.assertTrue(all(m.descrizione == f"Movimento {i} riga due {i}" for i, m in enumerate(movimenti, 1)))

    def test_sumup_pdf(self):
        movimenti, _ = parse_estratto("sumup.pdf", campioni.pdf_sumup(), "sumup")
        self.assertEqual([m.importo for m in movimenti], [D("486.35"), D("688.04"), D("-1.50")])
        self.assertIn("02.02.2026", movimenti[0].descrizione)

    def test_vivid_pdf(self):
        movimenti, _ = parse_estratto("vivid.pdf", campioni.pdf_vivid(), "vivid")
        self.assertEqual([m.importo for m in movimenti], [D("-49.90"), D("305.00")])
        self.assertEqual(movimenti[1].data_valuta, date(2026, 2, 11))
        self.assertIn("Rechnung 2026/18", movimenti[1].descrizione)

    def test_revolut_csv(self):
        movimenti, _ = parse_estratto("rev.csv", campioni.CSV_REVOLUT.encode())
        self.assertEqual(len(movimenti), 2)  # il PENDING è escluso
        self.assertEqual(movimenti[0].importo, D("1220.00"))
        self.assertIn("Saldo fattura 2026/15", movimenti[0].descrizione)
