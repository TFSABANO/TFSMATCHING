"""Import corrispettivi da export Excel/CSV del gestionale.

Due layout supportati, riconosciuti dalle intestazioni:

1. "lungo": una riga per incasso/metodo
       Data | Importo | Metodo pagamento | Riferimento
2. "largo": una riga per giorno con una colonna per metodo
       Data | Contanti | Carte | Bancomat | Satispay | Totale

Le intestazioni vengono confrontate con liste di sinonimi; se il gestionale usa nomi diversi
basta aggiungerli alle costanti qui sotto.
"""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .util import normalizza_testo, parse_data, parse_importo

SIN_DATA = ["data", "data documento", "data chiusura", "giorno", "data corrispettivo", "date"]
SIN_IMPORTO = ["importo", "totale", "totale corrispettivo", "incasso", "ammontare", "importo lordo", "amount"]
SIN_METODO = ["metodo", "metodo pagamento", "pagamento", "tipo pagamento", "modalita pagamento", "forma pagamento"]
SIN_RIF = ["riferimento", "numero", "n. documento", "numero documento", "scontrino", "chiusura", "n. chiusura", "cassa"]
SIN_DESCR = ["descrizione", "note", "causale"]

# colonne del layout "largo" -> metodo
COLONNE_METODO = {
    "contanti": "contanti",
    "cassa contanti": "contanti",
    "carta": "carta",
    "carte": "carta",
    "pos": "carta",
    "bancomat": "carta",
    "carta di credito": "carta",
    "elettronico": "carta",
    "pagamento elettronico": "carta",
    "sumup": "carta",
    "satispay": "altro",
    "bonifico": "bonifico",
}


@dataclass
class RigaCorrispettivo:
    data: date
    metodo: str
    importo: Decimal
    riferimento: str = ""
    descrizione: str = ""


def _chiave(s):
    s = normalizza_testo(str(s or "")).lower()
    s = s.replace("à", "a").replace("è", "e").replace("é", "e").replace("ò", "o").replace("ù", "u").replace("ì", "i")
    return re.sub(r"[^a-z0-9. ]", "", s).strip()


def normalizza_metodo(valore: str) -> str:
    v = _chiave(valore)
    if not v:
        return "carta"
    if "contant" in v or v in ("cash", "mp01"):
        return "contanti"
    if any(k in v for k in ("carta", "pos", "bancomat", "credit", "debit", "sumup", "visa", "mastercard", "amex", "mp08")):
        return "carta"
    if "bonific" in v or v == "mp05":
        return "bonifico"
    return "altro"


def leggi_tabella(nome_file: str, contenuto: bytes) -> list[list]:
    """Restituisce le righe grezze (lista di liste) da xlsx/xls/csv."""
    nome = nome_file.lower()
    if nome.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(contenuto), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if nome.endswith(".xls"):
        raise ValueError("Formato .xls non supportato: salvare il file come .xlsx o .csv")
    testo = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            testo = contenuto.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(testo[:4096], delimiters=";,\t|")
    return [r for r in csv.reader(io.StringIO(testo), dialect)]


def _trova_intestazione(righe):
    """Cerca nelle prime 20 righe quella che contiene una colonna data."""
    for i, r in enumerate(righe[:20]):
        chiavi = [_chiave(c) for c in r]
        if any(k in SIN_DATA for k in chiavi):
            return i, chiavi
    raise ValueError("Intestazione non trovata: serve una colonna 'Data'")


def _indice(chiavi, sinonimi):
    for s in sinonimi:
        if s in chiavi:
            return chiavi.index(s)
    for i, k in enumerate(chiavi):  # match parziale
        if any(k.startswith(s) for s in sinonimi):
            return i
    return None


def parse_corrispettivi(nome_file: str, contenuto: bytes, metodo_default: str = "carta") -> tuple[list[RigaCorrispettivo], list[str]]:
    righe = leggi_tabella(nome_file, contenuto)
    idx_head, chiavi = _trova_intestazione(righe)
    i_data = _indice(chiavi, SIN_DATA)
    i_imp = _indice(chiavi, SIN_IMPORTO)
    i_met = _indice(chiavi, SIN_METODO)
    i_rif = _indice(chiavi, SIN_RIF)
    i_des = _indice(chiavi, SIN_DESCR)
    colonne_metodo = {i: COLONNE_METODO[k] for i, k in enumerate(chiavi) if k in COLONNE_METODO}

    risultati, avvisi = [], []
    for n, r in enumerate(righe[idx_head + 1 :], start=idx_head + 2):
        if not r or all(c in (None, "") for c in r):
            continue
        d = parse_data(r[i_data] if i_data < len(r) else None)
        if not d:
            avvisi.append(f"Riga {n}: data non valida, saltata")
            continue
        rif = str(r[i_rif]).strip() if i_rif is not None and i_rif < len(r) and r[i_rif] is not None else ""
        des = str(r[i_des]).strip() if i_des is not None and i_des < len(r) and r[i_des] is not None else ""

        if colonne_metodo and i_met is None:
            # layout largo: una riga per ogni metodo con importo != 0
            for i, metodo in colonne_metodo.items():
                imp = parse_importo(r[i]) if i < len(r) else None
                if imp:
                    risultati.append(RigaCorrispettivo(d, metodo, imp, rif, des))
            continue

        if i_imp is None:
            raise ValueError("Colonna importo non trovata (attese: " + ", ".join(SIN_IMPORTO) + ")")
        imp = parse_importo(r[i_imp]) if i_imp < len(r) else None
        if not imp:
            avvisi.append(f"Riga {n}: importo mancante o zero, saltata")
            continue
        metodo = normalizza_metodo(r[i_met]) if i_met is not None and i_met < len(r) else metodo_default
        risultati.append(RigaCorrispettivo(d, metodo, imp, rif, des))
    return risultati, avvisi
