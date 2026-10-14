from .models import Membro


def azienda(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "azienda": getattr(request, "azienda", None),
        "membro": getattr(request, "membro", None),
        "aziende_utente": [m.azienda for m in Membro.objects.filter(utente=request.user).select_related("azienda")],
    }
