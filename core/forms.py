from django import forms
from django.contrib.auth import get_user_model

from .models import Azienda, ContoBancario, CredenzialiAruba, Membro


class StileBootstrap:
    """Aggiunge le classi Bootstrap ai widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            w = campo.widget
            classe = "form-select" if isinstance(w, forms.Select) else "form-control"
            w.attrs["class"] = (w.attrs.get("class", "") + " " + classe).strip()


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    widget = MultiFileInput

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data]
        return [single(data, initial)]


class CorrispettiviForm(StileBootstrap, forms.Form):
    file = MultiFileField(label="File Excel/CSV dal gestionale")
    metodo_default = forms.ChoiceField(
        label="Metodo se non indicato nel file",
        choices=[("carta", "Carta / POS"), ("contanti", "Contanti"), ("altro", "Altro elettronico")],
    )


class EstrattoForm(StileBootstrap, forms.Form):
    conto = forms.ModelChoiceField(queryset=ContoBancario.objects.none(), label="Conto")
    file = MultiFileField(label="Estratti conto (PDF, CSV o Excel)")

    def __init__(self, *args, azienda=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conto"].queryset = ContoBancario.objects.filter(azienda=azienda)


class FattureFileForm(StileBootstrap, forms.Form):
    file = MultiFileField(label="Fatture (XML, P7M o ZIP esportati da Aruba)")
    direzione = forms.ChoiceField(
        label="Se l'azienda non è riconoscibile dalla P.IVA",
        choices=[("", "Riconosci automaticamente"), ("attiva", "Sono fatture emesse"), ("passiva", "Sono fatture ricevute")],
        required=False,
    )


class SyncArubaForm(StileBootstrap, forms.Form):
    dal = forms.DateField(label="Dal", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    al = forms.DateField(label="Al", required=False, widget=forms.DateInput(attrs={"type": "date"}))


class AziendaForm(StileBootstrap, forms.ModelForm):
    class Meta:
        model = Azienda
        fields = ["ragione_sociale", "partita_iva", "codice_fiscale"]


class ContoForm(StileBootstrap, forms.ModelForm):
    class Meta:
        model = ContoBancario
        fields = ["nome", "banca", "iban"]


class ArubaForm(StileBootstrap, forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False,
                               help_text="Lasciare vuoto per non modificarla")

    class Meta:
        model = CredenzialiAruba
        fields = ["username", "ambiente"]

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("password"):
            obj.password = self.cleaned_data["password"]
        if commit:
            obj.save()
        return obj


class MembroForm(StileBootstrap, forms.Form):
    username = forms.CharField(label="Username o email dell'utente")
    ruolo = forms.ChoiceField(choices=Membro.RUOLI)

    def clean_username(self):
        v = self.cleaned_data["username"].strip()
        User = get_user_model()
        utente = User.objects.filter(username=v).first() or User.objects.filter(email__iexact=v).first()
        if not utente:
            raise forms.ValidationError("Utente non trovato: va prima creato dall'amministratore (pannello /admin).")
        return utente


class AbbinamentoManualeForm(forms.Form):
    tipo = forms.ChoiceField(choices=[("f", "Fattura"), ("c", "Corrispettivo")])
    documento_id = forms.IntegerField()
    importo = forms.DecimalField(max_digits=14, decimal_places=2, label="Quota movimento")
    importo_documento = forms.DecimalField(max_digits=14, decimal_places=2, required=False, label="Quota documento")
