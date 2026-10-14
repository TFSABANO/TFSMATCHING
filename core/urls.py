from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("azienda/cambia/", views.cambia_azienda, name="cambia_azienda"),
    path("importa/", views.importa, name="importa"),
    path("importazioni/", views.importazioni, name="importazioni"),
    path("riconciliazione/", views.riconciliazione, name="riconciliazione"),
    path("riconciliazione/proposte/", views.genera_proposte, name="genera_proposte"),
    path("riconciliazione/conferma-soglia/", views.conferma_soglia, name="conferma_soglia"),
    path("abbinamento/<int:pk>/<str:azione>/", views.azione_abbinamento, name="azione_abbinamento"),
    path("movimento/<int:pk>/", views.movimento, name="movimento"),
    path("fatture/", views.fatture, name="fatture"),
    path("corrispettivi/", views.corrispettivi, name="corrispettivi"),
    path("esporta/", views.esporta, name="esporta"),
    path("impostazioni/", views.impostazioni, name="impostazioni"),
]
