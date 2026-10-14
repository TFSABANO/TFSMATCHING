# Riconcilia

Web app multiutente per la riconciliazione bancaria:

- **corrispettivi** dal gestionale (export Excel/CSV) ↔ **accrediti POS** (SumUp ecc.), con commissioni e accrediti cumulativi di più giorni;
- **fatture emesse** (Aruba Fatturazione Elettronica) ↔ **incassi** sugli estratti conto;
- **fatture ricevute** ↔ **pagamenti**;
- estratti conto **PDF** di Revolut Business, Vivid, SumUp (oppure CSV/Excel esportati dalla banca);
- fatture da **API Aruba** oppure da **ZIP/XML/P7M** scaricati dal pannello.

Il sistema propone gli abbinamenti con un punteggio; l'operatore conferma, scarta o abbina a mano, poi esporta tutto in Excel.

## Avvio rapido (sviluppo)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py crea_demo --file-dir esempi   # azienda demo + file di esempio in ./esempi
python manage.py runserver
```

Apri http://127.0.0.1:8000 e accedi con `demo` / `demo12345`.

Test: `python manage.py test core`

## Produzione (Docker + PostgreSQL)

```bash
cp .env.example .env        # impostare SECRET_KEY, host, password DB, FIELD_ENCRYPTION_KEY
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Mettere davanti un reverse proxy HTTPS (nginx, Caddy, Traefik). Il servizio `cron` scarica ogni 24 ore le nuove
fatture da Aruba per tutte le aziende configurate e ricalcola le proposte (`python manage.py sincronizza_aruba`).

## Configurazione iniziale

1. **/admin** (superuser): creare gli utenti e l'azienda, associando almeno un utente come *Amministratore*.
2. **Impostazioni** (amministratore dell'azienda):
   - P.IVA/codice fiscale: servono a capire se una fattura è emessa o ricevuta;
   - conti bancari (Revolut / Vivid / SumUp);
   - credenziali Aruba (salvate cifrate). Sull'account Aruba deve essere abilitato l'accesso API;
     per provare usare l'ambiente *Demo*;
   - utenti dell'azienda con ruolo: *Amministratore*, *Operatore* (importa e abbina), *Sola lettura*.
3. **Importa**: caricare corrispettivi, estratti conto e fatture (o sincronizzare da Aruba).
4. **Riconciliazione** → *Calcola abbinamenti* → verificare e confermare → *Esporta Excel*.

## Come funziona l'abbinamento

| Regola | Punti |
|---|---|
| Importo uguale al residuo della fattura (± 0,05 €) | +50 |
| Numero fattura nella causale (`2026/15`, `15/2026`, `ft 15`, `fattura n. 15`…) | +35 |
| Ragione sociale della controparte nella causale | +25 (parziale +10) |
| Movimento entro ±10 gg dalla scadenza / entro 60 gg dalla fattura | +10 / +5 |
| Bonifico unico per più fatture della stessa controparte (fino a 4) | 60–90 |
| Accredito POS = lordo di uno o più giorni consecutivi meno commissione 0–3,5% | 60–95 |

- Il netto atteso tiene conto di **ritenuta d'acconto**, **split payment** e importi in `DatiPagamento`.
- Note di credito: un incasso può chiudere una nota di credito ricevuta, un pagamento una nota emessa.
- I pagamenti parziali sono gestiti: residuo su fattura e movimento.
- Le proposte scartate non vengono più riproposte; quelle confermate non vengono mai toccate dal ricalcolo.
- Con *Conferma proposte ≥ 90* si confermano in blocco gli abbinamenti sicuri.

I parametri (giorni, tolleranze, commissione massima POS, soglia) si cambiano con le variabili `RIC_*` in `.env`.

## Formati supportati

**Corrispettivi** (`core/parsers/corrispettivi.py`): Excel/CSV, intestazione cercata nelle prime 20 righe.
- Formato "lungo": `Data | Importo | Metodo pagamento | Riferimento`;
- Formato "largo": `Data | Contanti | Carte | Bancomat | Satispay | …`.
Se il gestionale usa nomi di colonna diversi basta aggiungerli alle liste `SIN_*` / `COLONNE_METODO`.

**Estratti conto** (`core/parsers/estratti.py`):
- PDF: il parser trova l'intestazione della tabella (Date/Data/Datum, Money in/out, Importo/Betrag, Saldo…),
  usa la posizione delle colonne per assegnare gli importi e controlla i segni con il saldo progressivo;
- CSV/Excel: export di Revolut (esclude PENDING/REVERTED, sottrae le commissioni), Vivid, SumUp o generici.

**Fatture**: FatturaPA 1.2 (XML, XML.P7M CAdES, ZIP anche annidati, lotti con più body); i file di servizio SdI
(`_MT_`, `_RC_`, `_NS_`…) vengono ignorati.

## Struttura

```
config/            impostazioni Django
core/models.py     Azienda, Membro, ContoBancario, MovimentoBancario, Fattura, Corrispettivo, Abbinamento
core/parsers/      lettura corrispettivi, estratti conto, FatturaPA
core/aruba.py      client API Aruba Fatturazione Elettronica
core/servizi.py    import con deduplica
core/matching.py   motore di abbinamento
core/export.py     export Excel
core/tests/        test e generatori di file di esempio
```
