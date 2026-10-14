from .models import Membro


class AziendaCorrenteMiddleware:
    """Imposta request.azienda e request.membro in base all'azienda scelta in sessione."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.azienda = None
        request.membro = None
        if request.user.is_authenticated:
            membri = Membro.objects.filter(utente=request.user).select_related("azienda")
            scelta = request.session.get("azienda_id")
            membro = next((m for m in membri if m.azienda_id == scelta), None) or next(iter(membri), None)
            if membro:
                request.azienda = membro.azienda
                request.membro = membro
                request.session["azienda_id"] = membro.azienda_id
        return self.get_response(request)
