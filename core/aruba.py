"""Client per le API REST di Aruba Fatturazione Elettronica.

Riferimento: documentazione "Aruba Fatturazione Elettronica - API" (fatturazioneelettronica.aruba.it).
Flusso:
  1. POST {auth}/auth/signin (grant_type=password) -> access_token
  2. GET  {ws}/services/invoice/out/findByUsername  -> fatture emesse (paginato)
     GET  {ws}/services/invoice/in/findByUsername   -> fatture ricevute (paginato)
  3. GET  {ws}/services/invoice/{out|in}/getByFilename?filename=...&includeFile=true -> XML/P7M in base64

Le API richiedono che sull'account Aruba sia attivo l'accesso API (vedi il pannello Aruba).
Usare ARUBA_ENV=demo per provare con l'ambiente di test.
"""
import base64
import logging
from datetime import date, datetime, time, timedelta

import requests

log = logging.getLogger(__name__)

ENDPOINTS = {
    "prod": ("https://auth.fatturazioneelettronica.aruba.it", "https://ws.fatturazioneelettronica.aruba.it"),
    "demo": ("https://demoauth.fatturazioneelettronica.aruba.it", "https://demows.fatturazioneelettronica.aruba.it"),
}


class ArubaErrore(Exception):
    pass


class ArubaClient:
    def __init__(self, username: str, password: str, ambiente: str = "prod", timeout: int = 60, session=None):
        self.username = username
        self.password = password
        self.auth_url, self.ws_url = ENDPOINTS[ambiente]
        self.timeout = timeout
        self.http = session or requests.Session()
        self._token = None

    # ------------------------------------------------------------------ auth
    def login(self):
        r = self.http.post(
            f"{self.auth_url}/auth/signin",
            data={"grant_type": "password", "username": self.username, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise ArubaErrore(f"Login Aruba fallito ({r.status_code}): {r.text[:200]}")
        self._token = r.json().get("access_token")
        if not self._token:
            raise ArubaErrore("Login Aruba: access_token assente nella risposta")

    def _get(self, path, params):
        if not self._token:
            self.login()
        for tentativo in range(2):
            r = self.http.get(
                f"{self.ws_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=self.timeout,
            )
            if r.status_code == 401 and tentativo == 0:
                self.login()  # token scaduto
                continue
            if r.status_code != 200:
                raise ArubaErrore(f"Errore API Aruba {path} ({r.status_code}): {r.text[:200]}")
            return r.json()

    # ------------------------------------------------------------------ fatture
    @staticmethod
    def _fmt(d: date, fine=False) -> str:
        dt = datetime.combine(d, time.max if fine else time.min)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def elenco(self, direzione: str, dal: date, al: date, dimensione_pagina: int = 100):
        """direzione: 'out' (emesse) o 'in' (ricevute). Genera i record dell'elenco Aruba.

        Aruba limita l'intervallo di date per singola ricerca: si interroga a blocchi di 90 giorni.
        """
        inizio = dal
        while inizio <= al:
            fine = min(inizio + timedelta(days=89), al)
            pagina = 1
            while True:
                dati = self._get(
                    f"/services/invoice/{direzione}/findByUsername",
                    {
                        "username": self.username,
                        "page": pagina,
                        "size": dimensione_pagina,
                        "startDate": self._fmt(inizio),
                        "endDate": self._fmt(fine, fine=True),
                    },
                )
                for rec in dati.get("content", []):
                    yield rec
                totale_pagine = dati.get("totalPages") or 1
                if pagina >= totale_pagine or not dati.get("content"):
                    break
                pagina += 1
            inizio = fine + timedelta(days=1)

    def scarica_file(self, direzione: str, filename: str) -> bytes:
        dati = self._get(
            f"/services/invoice/{direzione}/getByFilename",
            {"filename": filename, "includeFile": "true", "includePdf": "false"},
        )
        contenuto = dati.get("file")
        if not contenuto:
            raise ArubaErrore(f"File {filename} non restituito da Aruba")
        return base64.b64decode(contenuto)
