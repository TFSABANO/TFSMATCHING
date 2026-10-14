import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MESI = {
    # italiano
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
    "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
    # inglese
    "jan": 1, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "dec": 12,
    # tedesco (Vivid)
    "jän": 1, "mär": 3, "mai": 5, "okt": 10, "dez": 12,
}



def parse_importo(valore) -> Decimal | None:
    """Converte importi in formato italiano, inglese o tedesco in Decimal.

    '1.234,56' -> 1234.56, '1,234.56' -> 1234.56, '-12,5 €' -> -12.50, '(12.50)' -> -12.50
    """
    if valore is None:
        return None
    if isinstance(valore, (int, float, Decimal)):
        return Decimal(str(valore)).quantize(Decimal("0.01"))
    s = str(valore).strip()
    if not s:
        return None
    negativo = False
    if s.startswith("(") and s.endswith(")"):
        negativo, s = True, s[1:-1]
    s = s.replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "").replace("'", "")
    if s.endswith("-"):
        negativo, s = True, s[:-1]
    if s.startswith("-") or s.startswith("−"):
        negativo, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not re.fullmatch(r"[\d.,]+", s):
        return None
    # Separatore decimale:
    # - se ci sono sia '.' sia ',' è decimale l'ultimo dei due;
    # - se c'è un solo tipo ripetuto più volte è separatore delle migliaia;
    # - se compare una sola volta è decimale, salvo che sia seguito da esattamente 3 cifre (migliaia).
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        dec = sep if s.count(sep) == 1 and len(s) - s.rfind(sep) - 1 != 3 else None
    else:
        dec = None
    if dec == ",":
        s = s.replace(".", "").replace(",", ".")
    elif dec == ".":
        s = s.replace(",", "")
    else:
        s = s.replace(",", "").replace(".", "")
    try:
        d = Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -d if negativo else d


def parse_data(valore) -> date | None:
    """Accetta date/datetime, 'gg/mm/aaaa', 'gg.mm.aaaa', 'aaaa-mm-gg', '3 Jan 2026', '03 gen 2026', ecc."""
    if valore is None or valore == "":
        return None
    if isinstance(valore, datetime):
        return valore.date()
    if isinstance(valore, date):
        return valore
    s = str(valore).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
    if m:
        anno = int(m[3])
        if anno < 100:
            anno += 2000
        return _safe_date(anno, int(m[2]), int(m[1]))
    m = re.match(r"^(\d{1,2})\.?\s+([A-Za-zÀ-ÿ]{3,})\.?\s+(\d{4})", s)
    if m:
        mese = MESI.get(m[2][:3].lower())
        if mese:
            return _safe_date(int(m[3]), mese, int(m[1]))
    m = re.match(r"^([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4})", s)  # 'Jan 3, 2026'
    if m:
        mese = MESI.get(m[1][:3].lower())
        if mese:
            return _safe_date(int(m[3]), mese, int(m[2]))
    return None


def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def impronta(*parti) -> str:
    return hashlib.sha256("|".join(str(p) for p in parti).encode()).hexdigest()


def normalizza_testo(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()
