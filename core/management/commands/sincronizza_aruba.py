"""Sincronizza le fatture da Aruba e ricalcola le proposte, per tutte le aziende configurate.

Da pianificare con cron, ad es. ogni notte:
    0 3 * * *  cd /app && python manage.py sincronizza_aruba
"""
from django.core.management.base import BaseCommand

from core import matching, servizi
from core.models import CredenzialiAruba


class Command(BaseCommand):
    help = "Scarica le nuove fatture da Aruba Fatturazione Elettronica e aggiorna le proposte di abbinamento"

    def add_arguments(self, parser):
        parser.add_argument("--azienda", type=int, help="id azienda (default: tutte)")
        parser.add_argument("--dal", help="data iniziale AAAA-MM-GG")
        parser.add_argument("--al", help="data finale AAAA-MM-GG")

    def handle(self, *args, **opts):
        from datetime import date

        qs = CredenzialiAruba.objects.select_related("azienda")
        if opts["azienda"]:
            qs = qs.filter(azienda_id=opts["azienda"])
        dal = date.fromisoformat(opts["dal"]) if opts["dal"] else None
        al = date.fromisoformat(opts["al"]) if opts["al"] else None
        for cred in qs:
            try:
                imp = servizi.sincronizza_aruba(cred.azienda, dal, al)
                n = matching.genera_proposte(cred.azienda)
                self.stdout.write(f"{cred.azienda}: {imp.righe_importate} nuove fatture, {n} proposte")
                if imp.note:
                    self.stderr.write(imp.note)
            except Exception as exc:
                self.stderr.write(f"{cred.azienda}: ERRORE {exc}")
