from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum

from .crypto import decrypt, encrypt


class Azienda(models.Model):
    """Tenant: ogni dato appartiene a un'azienda; gli utenti vedono solo le proprie aziende."""

    ragione_sociale = models.CharField(max_length=200)
    partita_iva = models.CharField(max_length=20, blank=True)
    codice_fiscale = models.CharField(max_length=20, blank=True)
    utenti = models.ManyToManyField(settings.AUTH_USER_MODEL, through="Membro", related_name="aziende")

    class Meta:
        verbose_name_plural = "aziende"
        ordering = ["ragione_sociale"]

    def __str__(self):
        return self.ragione_sociale

    def identificativi(self):
        """Set di P.IVA/CF dell'azienda normalizzati, per capire se una fattura è attiva o passiva."""
        return {n for n in (normalizza_id_fiscale(self.partita_iva), normalizza_id_fiscale(self.codice_fiscale)) if n}


def normalizza_id_fiscale(valore):
    """'IT 01234567890' -> '01234567890'; i codici fiscali restano invariati (maiuscoli)."""
    v = (valore or "").upper().replace(" ", "").strip()
    if v.startswith("IT") and len(v) == 13 and v[2:].isdigit():
        v = v[2:]
    return v


class Membro(models.Model):
    RUOLI = [("admin", "Amministratore"), ("operatore", "Operatore"), ("lettura", "Sola lettura")]
    utente = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE)
    ruolo = models.CharField(max_length=20, choices=RUOLI, default="operatore")

    class Meta:
        unique_together = [("utente", "azienda")]
        verbose_name_plural = "membri"

    def __str__(self):
        return f"{self.utente} @ {self.azienda} ({self.ruolo})"

    @property
    def puo_modificare(self):
        return self.ruolo in ("admin", "operatore")


class CredenzialiAruba(models.Model):
    azienda = models.OneToOneField(Azienda, on_delete=models.CASCADE, related_name="aruba")
    username = models.CharField(max_length=200)
    _password = models.TextField(db_column="password")
    ambiente = models.CharField(max_length=10, choices=[("prod", "Produzione"), ("demo", "Demo")], default="prod")
    ultima_sincronizzazione = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "credenziali Aruba"

    @property
    def password(self):
        return decrypt(self._password)

    @password.setter
    def password(self, value):
        self._password = encrypt(value)


class ContoBancario(models.Model):
    BANCHE = [
        ("revolut", "Revolut Business"),
        ("vivid", "Vivid Money"),
        ("sumup", "SumUp conto aziendale"),
        ("generico", "Altro (parser generico)"),
    ]
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE, related_name="conti")
    nome = models.CharField(max_length=100)
    banca = models.CharField(max_length=20, choices=BANCHE)
    iban = models.CharField(max_length=34, blank=True)

    class Meta:
        verbose_name_plural = "conti bancari"
        ordering = ["nome"]

    def __str__(self):
        return f"{self.nome} ({self.get_banca_display()})"


class Importazione(models.Model):
    TIPI = [
        ("corrispettivi", "Corrispettivi"),
        ("estratto", "Estratto conto"),
        ("fatture_file", "Fatture da file"),
        ("fatture_aruba", "Fatture da API Aruba"),
    ]
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE, related_name="importazioni")
    tipo = models.CharField(max_length=20, choices=TIPI)
    nome_file = models.CharField(max_length=255, blank=True)
    utente = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    creata_il = models.DateTimeField(auto_now_add=True)
    righe_importate = models.IntegerField(default=0)
    righe_saltate = models.IntegerField(default=0)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-creata_il"]
        verbose_name_plural = "importazioni"


class _ConResiduo(models.Model):
    """Mixin per documenti che possono essere coperti (anche parzialmente) da movimenti."""

    class Meta:
        abstract = True

    campo_quota = "importo_documento"

    def importo_abbinato(self, solo_confermati=True):
        qs = self.abbinamenti.all()
        if solo_confermati:
            qs = qs.filter(stato=Abbinamento.CONFERMATO)
        return qs.aggregate(t=Sum(self.campo_quota))["t"] or Decimal("0")

    @property
    def residuo(self):
        return self.importo_da_abbinare - self.importo_abbinato()


class MovimentoBancario(_ConResiduo):
    campo_quota = "importo"
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE, related_name="movimenti")
    conto = models.ForeignKey(ContoBancario, on_delete=models.CASCADE, related_name="movimenti")
    importazione = models.ForeignKey(Importazione, null=True, blank=True, on_delete=models.SET_NULL)
    data = models.DateField()
    data_valuta = models.DateField(null=True, blank=True)
    descrizione = models.TextField()
    importo = models.DecimalField(max_digits=14, decimal_places=2, help_text="positivo = entrata, negativo = uscita")
    saldo = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    impronta = models.CharField(max_length=64, help_text="hash per evitare doppie importazioni")

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name_plural = "movimenti bancari"
        constraints = [models.UniqueConstraint(fields=["conto", "impronta"], name="mov_unico_per_conto")]

    def __str__(self):
        return f"{self.data} {self.importo} {self.descrizione[:40]}"

    @property
    def importo_da_abbinare(self):
        return abs(self.importo)

    @property
    def entrata(self):
        return self.importo > 0


class Fattura(_ConResiduo):
    ATTIVA, PASSIVA = "attiva", "passiva"
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE, related_name="fatture")
    importazione = models.ForeignKey(Importazione, null=True, blank=True, on_delete=models.SET_NULL)
    direzione = models.CharField(max_length=10, choices=[(ATTIVA, "Attiva (emessa)"), (PASSIVA, "Passiva (ricevuta)")])
    tipo_documento = models.CharField(max_length=4, default="TD01", help_text="TD01 fattura, TD04 nota di credito, ...")
    numero = models.CharField(max_length=50)
    data = models.DateField()
    controparte = models.CharField(max_length=255)
    controparte_piva = models.CharField(max_length=30, blank=True)
    imponibile = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    imposta = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    totale = models.DecimalField(max_digits=14, decimal_places=2)
    netto_a_pagare = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="totale meno ritenute / split payment: quanto ci si aspetta in banca"
    )
    data_scadenza = models.DateField(null=True, blank=True)
    modalita_pagamento = models.CharField(max_length=10, blank=True, help_text="MP01 contanti, MP05 bonifico, MP08 carta ...")
    iban_pagamento = models.CharField(max_length=34, blank=True)
    nome_file_sdi = models.CharField(max_length=255, blank=True)
    id_sdi = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name_plural = "fatture"
        constraints = [
            models.UniqueConstraint(
                fields=["azienda", "direzione", "controparte_piva", "numero", "data"], name="fattura_unica"
            )
        ]

    def __str__(self):
        return f"{self.get_direzione_display()} n.{self.numero} del {self.data} - {self.controparte}"

    @property
    def importo_da_abbinare(self):
        return abs(self.netto_a_pagare)

    @property
    def nota_credito(self):
        return self.tipo_documento in ("TD04", "TD08")


class Corrispettivo(_ConResiduo):
    """Riga di corrispettivo giornaliero dal gestionale (tipicamente un totale per giorno e metodo di pagamento)."""

    METODI = [
        ("contanti", "Contanti"),
        ("carta", "Carta / POS"),
        ("bonifico", "Bonifico"),
        ("altro", "Altro elettronico"),
    ]
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE, related_name="corrispettivi")
    importazione = models.ForeignKey(Importazione, null=True, blank=True, on_delete=models.SET_NULL)
    data = models.DateField()
    metodo = models.CharField(max_length=20, choices=METODI, default="carta")
    importo = models.DecimalField(max_digits=14, decimal_places=2)
    riferimento = models.CharField(max_length=100, blank=True, help_text="n. scontrino/chiusura, cassa, ecc.")
    descrizione = models.CharField(max_length=255, blank=True)
    impronta = models.CharField(max_length=64)

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name_plural = "corrispettivi"
        constraints = [models.UniqueConstraint(fields=["azienda", "impronta"], name="corr_unico")]

    def __str__(self):
        return f"{self.data} {self.get_metodo_display()} {self.importo}"

    @property
    def importo_da_abbinare(self):
        return self.importo


class Abbinamento(models.Model):
    PROPOSTO, CONFERMATO, RIFIUTATO = "proposto", "confermato", "rifiutato"
    azienda = models.ForeignKey(Azienda, on_delete=models.CASCADE, related_name="abbinamenti")
    movimento = models.ForeignKey(MovimentoBancario, on_delete=models.CASCADE, related_name="abbinamenti")
    fattura = models.ForeignKey(Fattura, null=True, blank=True, on_delete=models.CASCADE, related_name="abbinamenti")
    corrispettivo = models.ForeignKey(
        Corrispettivo, null=True, blank=True, on_delete=models.CASCADE, related_name="abbinamenti"
    )
    importo = models.DecimalField(max_digits=14, decimal_places=2, help_text="quota del movimento attribuita al documento")
    importo_documento = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="quota del documento coperta (lordo; differisce per commissioni POS)"
    )
    stato = models.CharField(max_length=12, choices=[(PROPOSTO, "Proposto"), (CONFERMATO, "Confermato"), (RIFIUTATO, "Rifiutato")])
    punteggio = models.IntegerField(default=0)
    motivo = models.CharField(max_length=255, blank=True)
    manuale = models.BooleanField(default=False)
    gruppo = models.CharField(max_length=36, blank=True, help_text="proposte che vanno confermate insieme (es. più giorni POS)")
    creato_da = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    creato_il = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-punteggio"]
        verbose_name_plural = "abbinamenti"
        constraints = [
            models.CheckConstraint(
                condition=(Q(fattura__isnull=False, corrispettivo__isnull=True) | Q(fattura__isnull=True, corrispettivo__isnull=False)),
                name="abb_un_solo_documento",
            )
        ]

    @property
    def documento(self):
        return self.fattura or self.corrispettivo
