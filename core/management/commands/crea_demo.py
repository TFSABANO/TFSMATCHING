"""Crea un'azienda dimostrativa con utente, conti e dati di esempio (richiede reportlab, vedi requirements-dev.txt).

    python manage.py crea_demo                 # utente demo / password demo12345
    python manage.py crea_demo --file-dir esempi  # salva anche i file di esempio per provare l'upload
"""
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core import matching, servizi
from core.models import Azienda, ContoBancario, Membro
from core.tests import campioni
from core.tests.test_flusso import fatture_esempio


class Command(BaseCommand):
    help = "Crea dati dimostrativi"

    def add_arguments(self, parser):
        parser.add_argument("--file-dir", help="cartella dove salvare i file di esempio")

    def handle(self, *args, **opts):
        file = {
            "fatture_aruba.zip": fatture_esempio(),
            "corrispettivi.csv": campioni.CSV_CORRISPETTIVI_LUNGO.encode(),
            "corrispettivi_largo.xlsx": campioni.xlsx_corrispettivi_largo(),
            "estratto_revolut.pdf": campioni.pdf_revolut(),
            "estratto_sumup.pdf": campioni.pdf_sumup(),
            "estratto_vivid.pdf": campioni.pdf_vivid(),
            "revolut.csv": campioni.CSV_REVOLUT.encode(),
        }
        if opts["file_dir"]:
            cartella = Path(opts["file_dir"])
            cartella.mkdir(parents=True, exist_ok=True)
            for nome, dati in file.items():
                (cartella / nome).write_bytes(dati)
            self.stdout.write(f"File di esempio salvati in {cartella.resolve()}")

        utente, creato = User.objects.get_or_create(username="demo", defaults={"is_staff": True, "is_superuser": True})
        if creato:
            utente.set_password("demo12345")
            utente.save()
        az, _ = Azienda.objects.get_or_create(
            ragione_sociale="La Mia Azienda Srl (demo)", defaults={"partita_iva": campioni.PIVA_AZIENDA}
        )
        Membro.objects.get_or_create(utente=utente, azienda=az, defaults={"ruolo": "admin"})
        conti = {
            banca: ContoBancario.objects.get_or_create(azienda=az, banca=banca, defaults={"nome": nome})[0]
            for banca, nome in [("revolut", "Revolut Business"), ("sumup", "SumUp"), ("vivid", "Vivid Business")]
        }
        servizi.importa_fatture_file(az, "fatture_aruba.zip", file["fatture_aruba.zip"], utente)
        servizi.importa_corrispettivi(az, "corrispettivi.csv", file["corrispettivi.csv"], utente)
        servizi.importa_estratto(az, conti["revolut"], "estratto_revolut.pdf", file["estratto_revolut.pdf"], utente)
        servizi.importa_estratto(az, conti["sumup"], "estratto_sumup.pdf", file["estratto_sumup.pdf"], utente)
        servizi.importa_estratto(az, conti["vivid"], "estratto_vivid.pdf", file["estratto_vivid.pdf"], utente)
        n = matching.genera_proposte(az)
        self.stdout.write(self.style.SUCCESS(f"Demo pronta: {n} proposte. Accedi con demo / demo12345"))
