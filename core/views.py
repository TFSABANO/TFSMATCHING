from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import matching, servizi
from .aruba import ArubaErrore
from .export import esporta_riconciliazione
from .forms import (
    AbbinamentoManualeForm, ArubaForm, AziendaForm, ContoForm, CorrispettiviForm, EstrattoForm,
    FattureFileForm, MembroForm, SyncArubaForm,
)
from .models import (
    Abbinamento, ContoBancario, Corrispettivo, CredenzialiAruba, Fattura, Importazione, Membro, MovimentoBancario,
)


# ---------------------------------------------------------------- permessi

def con_azienda(ruolo_minimo="lettura"):
    """Richiede login, un'azienda associata e il ruolo indicato (lettura < operatore < admin)."""
    livelli = {"lettura": 0, "operatore": 1, "admin": 2}

    def deco(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.azienda is None:
                return render(request, "core/nessuna_azienda.html")
            if livelli[request.membro.ruolo] < livelli[ruolo_minimo]:
                return HttpResponseForbidden("Permessi insufficienti")
            return view(request, *args, **kwargs)

        return wrapper

    return deco


@login_required
@require_POST
def cambia_azienda(request):
    if Membro.objects.filter(utente=request.user, azienda_id=request.POST.get("azienda_id")).exists():
        request.session["azienda_id"] = int(request.POST["azienda_id"])
    return redirect("dashboard")


# ---------------------------------------------------------------- dashboard

def _annota_coperto(qs, campo="importo_documento"):
    return qs.annotate(
        coperto=Sum(f"abbinamenti__{campo}", filter=Q(abbinamenti__stato=Abbinamento.CONFERMATO), default=Decimal("0"))
    )


@con_azienda()
def dashboard(request):
    az = request.azienda
    movimenti = _annota_coperto(MovimentoBancario.objects.filter(azienda=az), "importo")
    mov_aperti = [m for m in movimenti if abs(m.importo) - m.coperto > Decimal("0.01")]
    fatture = _annota_coperto(Fattura.objects.filter(azienda=az))
    att_aperte = [f for f in fatture if f.direzione == "attiva" and f.importo_da_abbinare - f.coperto > Decimal("0.01")]
    pas_aperte = [f for f in fatture if f.direzione == "passiva" and f.importo_da_abbinare - f.coperto > Decimal("0.01")]
    corr = _annota_coperto(Corrispettivo.objects.filter(azienda=az).exclude(metodo="contanti"))
    corr_aperti = [c for c in corr if c.importo - c.coperto > Decimal("0.01")]
    ctx = {
        "n_movimenti": len(movimenti),
        "n_mov_aperti": len(mov_aperti),
        "n_proposte": Abbinamento.objects.filter(azienda=az, stato=Abbinamento.PROPOSTO).values("movimento").distinct().count(),
        "att_aperte": len(att_aperte),
        "att_aperte_tot": sum((f.importo_da_abbinare - f.coperto for f in att_aperte), Decimal("0")),
        "pas_aperte": len(pas_aperte),
        "pas_aperte_tot": sum((f.importo_da_abbinare - f.coperto for f in pas_aperte), Decimal("0")),
        "corr_aperti": len(corr_aperti),
        "corr_aperti_tot": sum((c.importo - c.coperto for c in corr_aperti), Decimal("0")),
        "importazioni": Importazione.objects.filter(azienda=az)[:10],
        "soglia": matching._cfg("SOGLIA_AUTO_CONFERMA"),
    }
    return render(request, "core/dashboard.html", ctx)


# ---------------------------------------------------------------- import

def _esegui_import(request, files, funzione, **kwargs):
    for f in files:
        try:
            imp = funzione(request.azienda, f.name, f.read(), utente=request.user, **kwargs)
            livello = messages.WARNING if imp.note else messages.SUCCESS
            messages.add_message(
                request, livello,
                f"{f.name}: {imp.righe_importate} nuovi, {imp.righe_saltate} già presenti/aggiornati"
                + (f" — {imp.note.count(chr(10)) + 1} avvisi (vedi Importazioni)" if imp.note else ""),
            )
        except Exception as exc:
            messages.error(request, f"{f.name}: {exc}")


@con_azienda("operatore")
def importa(request):
    az = request.azienda
    f_corr = CorrispettiviForm(prefix="corr")
    f_estr = EstrattoForm(prefix="estr", azienda=az)
    f_fatt = FattureFileForm(prefix="fatt")
    f_sync = SyncArubaForm(prefix="sync")
    if request.method == "POST":
        azione = request.POST.get("azione")
        if azione == "corrispettivi":
            f_corr = CorrispettiviForm(request.POST, request.FILES, prefix="corr")
            if f_corr.is_valid():
                _esegui_import(request, f_corr.cleaned_data["file"], servizi.importa_corrispettivi,
                               metodo_default=f_corr.cleaned_data["metodo_default"])
                return redirect("importa")
        elif azione == "estratto":
            f_estr = EstrattoForm(request.POST, request.FILES, prefix="estr", azienda=az)
            if f_estr.is_valid():
                conto = f_estr.cleaned_data["conto"]
                _esegui_import(request, f_estr.cleaned_data["file"],
                               lambda a, n, c, utente: servizi.importa_estratto(a, conto, n, c, utente))
                return redirect("importa")
        elif azione == "fatture":
            f_fatt = FattureFileForm(request.POST, request.FILES, prefix="fatt")
            if f_fatt.is_valid():
                _esegui_import(request, f_fatt.cleaned_data["file"], servizi.importa_fatture_file,
                               direzione_suggerita=f_fatt.cleaned_data["direzione"] or None)
                return redirect("importa")
        elif azione == "aruba":
            f_sync = SyncArubaForm(request.POST, prefix="sync")
            if f_sync.is_valid():
                if not CredenzialiAruba.objects.filter(azienda=az).exists():
                    messages.error(request, "Configura prima le credenziali Aruba nelle impostazioni.")
                else:
                    try:
                        imp = servizi.sincronizza_aruba(az, f_sync.cleaned_data["dal"], f_sync.cleaned_data["al"], request.user)
                        messages.success(request, f"Aruba: {imp.righe_importate} nuove fatture, {imp.righe_saltate} aggiornate")
                        if imp.note:
                            messages.warning(request, "Alcune fatture non sono state importate: vedi Importazioni")
                    except ArubaErrore as exc:
                        messages.error(request, str(exc))
                return redirect("importa")
    return render(request, "core/importa.html", {
        "f_corr": f_corr, "f_estr": f_estr, "f_fatt": f_fatt, "f_sync": f_sync,
        "ha_conti": ContoBancario.objects.filter(azienda=az).exists(),
        "aruba": CredenzialiAruba.objects.filter(azienda=az).first(),
    })


@con_azienda()
def importazioni(request):
    return render(request, "core/importazioni.html", {
        "importazioni": Paginator(Importazione.objects.filter(azienda=request.azienda), 50).get_page(request.GET.get("page"))
    })


# ---------------------------------------------------------------- riconciliazione

@con_azienda()
def riconciliazione(request):
    az = request.azienda
    stato = request.GET.get("stato", "proposti")
    qs = _annota_coperto(MovimentoBancario.objects.filter(azienda=az).select_related("conto"), "importo")
    qs = qs.annotate(n_proposte=Count("abbinamenti", filter=Q(abbinamenti__stato=Abbinamento.PROPOSTO)))
    if request.GET.get("conto"):
        qs = qs.filter(conto_id=request.GET["conto"])
    if request.GET.get("verso") == "entrate":
        qs = qs.filter(importo__gt=0)
    elif request.GET.get("verso") == "uscite":
        qs = qs.filter(importo__lt=0)
    if request.GET.get("q"):
        qs = qs.filter(descrizione__icontains=request.GET["q"])
    if request.GET.get("dal"):
        qs = qs.filter(data__gte=request.GET["dal"])
    if request.GET.get("al"):
        qs = qs.filter(data__lte=request.GET["al"])

    movimenti = list(qs)
    for m in movimenti:
        m.residuo_calc = abs(m.importo) - m.coperto
    if stato == "proposti":
        movimenti = [m for m in movimenti if m.n_proposte]
    elif stato == "aperti":
        movimenti = [m for m in movimenti if m.residuo_calc > Decimal("0.01")]
    elif stato == "abbinati":
        movimenti = [m for m in movimenti if m.residuo_calc <= Decimal("0.01")]

    pagina = Paginator(movimenti, 50).get_page(request.GET.get("page"))
    ids = [m.pk for m in pagina]
    abb = Abbinamento.objects.filter(movimento_id__in=ids).exclude(stato=Abbinamento.RIFIUTATO).select_related("fattura", "corrispettivo")
    per_mov = {}
    for a in abb:
        per_mov.setdefault(a.movimento_id, []).append(a)
    for m in pagina:
        m.lista_abbinamenti = per_mov.get(m.pk, [])
    filtri = request.GET.copy()
    filtri.pop("page", None)
    return render(request, "core/riconciliazione.html", {
        "pagina": pagina, "stato": stato, "conti": ContoBancario.objects.filter(azienda=az),
        "filtri": filtri.urlencode(), "soglia": matching._cfg("SOGLIA_AUTO_CONFERMA"),
    })


@con_azienda("operatore")
@require_POST
def genera_proposte(request):
    n = matching.genera_proposte(request.azienda)
    messages.success(request, f"Calcolate {n} nuove proposte di abbinamento.")
    return redirect(request.POST.get("next") or "riconciliazione")


@con_azienda("operatore")
@require_POST
def conferma_soglia(request):
    n = matching.conferma_sopra_soglia(request.azienda, request.user)
    messages.success(request, f"Confermati {n} abbinamenti con punteggio ≥ {matching._cfg('SOGLIA_AUTO_CONFERMA')}.")
    return redirect(request.POST.get("next") or "riconciliazione")


@con_azienda("operatore")
@require_POST
def azione_abbinamento(request, pk, azione):
    a = get_object_or_404(Abbinamento, pk=pk, azienda=request.azienda)
    if azione == "conferma":
        matching.conferma(a, request.user)
    elif azione == "rifiuta":
        matching.rifiuta(a, request.user)
    elif azione == "elimina":
        a.delete()
    return redirect(request.POST.get("next") or "riconciliazione")


@con_azienda()
def movimento(request, pk):
    az = request.azienda
    m = get_object_or_404(MovimentoBancario, pk=pk, azienda=az)
    if request.method == "POST":
        if not request.membro.puo_modificare:
            return HttpResponseForbidden()
        form = AbbinamentoManualeForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            doc = get_object_or_404(Fattura if d["tipo"] == "f" else Corrispettivo, pk=d["documento_id"], azienda=az)
            Abbinamento.objects.create(
                azienda=az, movimento=m,
                fattura=doc if d["tipo"] == "f" else None,
                corrispettivo=doc if d["tipo"] == "c" else None,
                importo=d["importo"], importo_documento=d["importo_documento"] or d["importo"],
                stato=Abbinamento.CONFERMATO, punteggio=100, manuale=True, motivo="abbinamento manuale",
                creato_da=request.user,
            )
            # le proposte automatiche residue sul movimento non servono più
            Abbinamento.objects.filter(movimento=m, stato=Abbinamento.PROPOSTO, manuale=False).delete()
            messages.success(request, "Abbinamento registrato.")
            return redirect("movimento", pk=m.pk)
        messages.error(request, f"Dati non validi: {form.errors.as_text()}")

    q = request.GET.get("q", "").strip()
    fatture = _annota_coperto(Fattura.objects.filter(azienda=az))
    corr = _annota_coperto(Corrispettivo.objects.filter(azienda=az).exclude(metodo="contanti"))
    nota_credito = Q(tipo_documento__in=["TD04", "TD08"])
    if m.importo > 0:  # incasso: fatture emesse o note di credito ricevute
        fatture = fatture.filter((Q(direzione="attiva") & ~nota_credito) | (Q(direzione="passiva") & nota_credito))
    else:  # pagamento: fatture ricevute o note di credito emesse
        fatture = fatture.filter((Q(direzione="passiva") & ~nota_credito) | (Q(direzione="attiva") & nota_credito))
        corr = corr.none()
    if q:
        fatture = fatture.filter(Q(numero__icontains=q) | Q(controparte__icontains=q) | Q(totale=_dec(q)) | Q(netto_a_pagare=_dec(q)))
        corr = corr.filter(Q(riferimento__icontains=q) | Q(importo=_dec(q)))
    else:
        # di default: documenti intorno alla data del movimento
        fatture = fatture.filter(data__lte=m.data + timezone.timedelta(days=5), data__gte=m.data - timezone.timedelta(days=180))
        corr = corr.filter(data__lte=m.data, data__gte=m.data - timezone.timedelta(days=10))
    fatture = [f for f in fatture.order_by("-data")[:200] if f.importo_da_abbinare - f.coperto > Decimal("0.01")][:50]
    corr = [c for c in corr.order_by("-data")[:200] if c.importo - c.coperto > Decimal("0.01")][:30]
    for f in fatture:
        f.residuo_calc = f.importo_da_abbinare - f.coperto
    for c in corr:
        c.residuo_calc = c.importo - c.coperto
    return render(request, "core/movimento.html", {
        "m": m, "residuo": m.residuo, "fatture": fatture, "corrispettivi": corr, "q": q,
        "abbinamenti": m.abbinamenti.exclude(stato=Abbinamento.RIFIUTATO).select_related("fattura", "corrispettivo"),
    })


def _dec(s):
    from .parsers.util import parse_importo

    return parse_importo(s) or Decimal("-999999999")


# ---------------------------------------------------------------- elenchi documenti

@con_azienda()
def fatture(request):
    qs = _annota_coperto(Fattura.objects.filter(azienda=request.azienda))
    direzione = request.GET.get("direzione", "")
    if direzione:
        qs = qs.filter(direzione=direzione)
    if request.GET.get("q"):
        q = request.GET["q"]
        qs = qs.filter(Q(numero__icontains=q) | Q(controparte__icontains=q) | Q(controparte_piva__icontains=q))
    elenco = list(qs.order_by("-data", "-id"))
    for f in elenco:
        f.residuo_calc = f.importo_da_abbinare - f.coperto
    stato = request.GET.get("stato", "")
    if stato == "aperte":
        elenco = [f for f in elenco if f.residuo_calc > Decimal("0.01")]
    elif stato == "pagate":
        elenco = [f for f in elenco if f.residuo_calc <= Decimal("0.01")]
    filtri = request.GET.copy()
    filtri.pop("page", None)
    return render(request, "core/fatture.html", {
        "pagina": Paginator(elenco, 50).get_page(request.GET.get("page")), "filtri": filtri.urlencode(),
    })


@con_azienda()
def corrispettivi(request):
    qs = _annota_coperto(Corrispettivo.objects.filter(azienda=request.azienda))
    if request.GET.get("metodo"):
        qs = qs.filter(metodo=request.GET["metodo"])
    elenco = list(qs.order_by("-data", "metodo"))
    for c in elenco:
        c.residuo_calc = c.importo - c.coperto
    if request.GET.get("stato") == "aperti":
        elenco = [c for c in elenco if c.metodo != "contanti" and c.residuo_calc > Decimal("0.01")]
    filtri = request.GET.copy()
    filtri.pop("page", None)
    return render(request, "core/corrispettivi.html", {
        "pagina": Paginator(elenco, 50).get_page(request.GET.get("page")), "filtri": filtri.urlencode(),
    })


@con_azienda()
def esporta(request):
    dati = esporta_riconciliazione(request.azienda, request.GET.get("dal") or None, request.GET.get("al") or None)
    nome = f"riconciliazione_{request.azienda.ragione_sociale[:30].replace(' ', '_')}_{timezone.localdate():%Y%m%d}.xlsx"
    resp = HttpResponse(dati, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{nome}"'
    return resp


# ---------------------------------------------------------------- impostazioni

@con_azienda("admin")
def impostazioni(request):
    az = request.azienda
    aruba = CredenzialiAruba.objects.filter(azienda=az).first()
    f_az = AziendaForm(instance=az, prefix="az")
    f_conto = ContoForm(prefix="conto")
    f_aruba = ArubaForm(instance=aruba, prefix="aruba")
    f_membro = MembroForm(prefix="membro")
    if request.method == "POST":
        azione = request.POST.get("azione")
        if azione == "azienda":
            f_az = AziendaForm(request.POST, instance=az, prefix="az")
            if f_az.is_valid():
                f_az.save()
                messages.success(request, "Dati azienda salvati.")
                return redirect("impostazioni")
        elif azione == "conto":
            f_conto = ContoForm(request.POST, prefix="conto")
            if f_conto.is_valid():
                conto = f_conto.save(commit=False)
                conto.azienda = az
                conto.save()
                messages.success(request, f"Conto {conto} aggiunto.")
                return redirect("impostazioni")
        elif azione == "elimina_conto":
            conto = get_object_or_404(ContoBancario, pk=request.POST.get("conto_id"), azienda=az)
            if conto.movimenti.exists():
                messages.error(request, "Il conto ha movimenti importati: eliminali prima dal pannello /admin.")
            else:
                conto.delete()
            return redirect("impostazioni")
        elif azione == "aruba":
            f_aruba = ArubaForm(request.POST, instance=aruba or CredenzialiAruba(azienda=az), prefix="aruba")
            if f_aruba.is_valid():
                if not aruba and not f_aruba.cleaned_data.get("password"):
                    f_aruba.add_error("password", "Obbligatoria al primo inserimento")
                else:
                    f_aruba.save()
                    messages.success(request, "Credenziali Aruba salvate (password cifrata).")
                    return redirect("impostazioni")
        elif azione == "membro":
            f_membro = MembroForm(request.POST, prefix="membro")
            if f_membro.is_valid():
                Membro.objects.update_or_create(
                    utente=f_membro.cleaned_data["username"], azienda=az, defaults={"ruolo": f_membro.cleaned_data["ruolo"]}
                )
                messages.success(request, "Utente associato all'azienda.")
                return redirect("impostazioni")
        elif azione == "rimuovi_membro":
            mb = get_object_or_404(Membro, pk=request.POST.get("membro_id"), azienda=az)
            if mb.utente_id == request.user.id:
                messages.error(request, "Non puoi rimuovere te stesso.")
            else:
                mb.delete()
            return redirect("impostazioni")
    return render(request, "core/impostazioni.html", {
        "f_az": f_az, "f_conto": f_conto, "f_aruba": f_aruba, "f_membro": f_membro, "aruba": aruba,
        "conti": ContoBancario.objects.filter(azienda=az).annotate(n=Count("movimenti")),
        "membri": Membro.objects.filter(azienda=az).select_related("utente"),
    })
