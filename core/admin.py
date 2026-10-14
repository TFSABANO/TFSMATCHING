from django.contrib import admin

from .models import (
    Abbinamento, Azienda, ContoBancario, Corrispettivo, CredenzialiAruba, Fattura, Importazione, Membro, MovimentoBancario,
)


class MembroInline(admin.TabularInline):
    model = Membro
    extra = 1


class ContoInline(admin.TabularInline):
    model = ContoBancario
    extra = 0


@admin.register(Azienda)
class AziendaAdmin(admin.ModelAdmin):
    list_display = ["ragione_sociale", "partita_iva", "codice_fiscale"]
    search_fields = ["ragione_sociale", "partita_iva"]
    inlines = [MembroInline, ContoInline]


@admin.register(CredenzialiAruba)
class CredenzialiArubaAdmin(admin.ModelAdmin):
    list_display = ["azienda", "username", "ambiente", "ultima_sincronizzazione"]
    exclude = ["_password"]  # la password si imposta dall'app (Impostazioni), cifrata


@admin.register(MovimentoBancario)
class MovimentoAdmin(admin.ModelAdmin):
    list_display = ["data", "conto", "descrizione", "importo", "saldo"]
    list_filter = ["azienda", "conto"]
    search_fields = ["descrizione"]
    date_hierarchy = "data"


@admin.register(Fattura)
class FatturaAdmin(admin.ModelAdmin):
    list_display = ["direzione", "numero", "data", "controparte", "totale", "netto_a_pagare", "data_scadenza"]
    list_filter = ["azienda", "direzione", "tipo_documento"]
    search_fields = ["numero", "controparte", "controparte_piva"]
    date_hierarchy = "data"


@admin.register(Corrispettivo)
class CorrispettivoAdmin(admin.ModelAdmin):
    list_display = ["data", "metodo", "importo", "riferimento"]
    list_filter = ["azienda", "metodo"]
    date_hierarchy = "data"


@admin.register(Abbinamento)
class AbbinamentoAdmin(admin.ModelAdmin):
    list_display = ["movimento", "fattura", "corrispettivo", "importo", "stato", "punteggio", "manuale"]
    list_filter = ["azienda", "stato", "manuale"]


@admin.register(Importazione)
class ImportazioneAdmin(admin.ModelAdmin):
    list_display = ["creata_il", "azienda", "tipo", "nome_file", "righe_importate", "righe_saltate", "utente"]
    list_filter = ["azienda", "tipo"]
