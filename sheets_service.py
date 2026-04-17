import logging
import time
import re

import gspread
from google.oauth2.service_account import Credentials

from config import settings

logger = logging.getLogger(__name__)

# Columns to extract for the AI prompt (safe, no sensitive data)
REPAIR_COLUMNS = [
    "resguardo",
    "fecha_recepcion",
    "cliente_nombre",
    "equipo_modelo",
    "sintoma",
    "estado",
    "presupuesto_aceptado_id",
    "tecnico_asignado",
    "fecha_reparacion",
    "resultado_reparacion",
    "fecha_entrega",
    "estado_entrega",
]

# Delivery states that mean the client no longer has a pending repair
CLOSED_DELIVERY_STATES = {"ENTREGADO", "RECICLAJE"}


def normalize_phone(phone: str) -> str:
    """Remove +, spaces, and leading zeros to normalize phone numbers."""
    return re.sub(r"[+\s\-]", "", phone).lstrip("0")


def phones_match(a: str, b: str) -> bool:
    """Compare two phone numbers, tolerating missing country code 34."""
    na, nb = normalize_phone(a), normalize_phone(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Try adding/removing Spanish country code (34)
    if na.startswith("34") and na[2:] == nb:
        return True
    if nb.startswith("34") and nb[2:] == na:
        return True
    return False


def _extract_repair(record: dict) -> dict:
    """Extract only safe columns from a sheet record."""
    repair = {}
    for col in REPAIR_COLUMNS:
        value = record.get(col, "")
        if value is not None and str(value).strip():
            repair[col] = str(value).strip()
    return repair


def _is_active(repair: dict) -> bool:
    """A repair is active if estado_entrega is not ENTREGADO or RECICLAJE."""
    estado = repair.get("estado_entrega", "").upper().strip()
    return estado not in CLOSED_DELIVERY_STATES


class SheetsService:
    """Service for fetching repair data and prices from Google Sheets."""

    def __init__(self):
        self._client: gspread.Client | None = None
        self._cache: list[dict] = []
        self._cache_time: float = 0
        self._prices_cache: list[dict] = []
        self._prices_cache_time: float = 0

    async def connect(self):
        """Authenticate with Google Sheets API using a service account."""
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets.readonly",
            ]
            creds = Credentials.from_service_account_file(
                settings.GOOGLE_CREDENTIALS_PATH, scopes=scopes
            )
            self._client = gspread.authorize(creds)
            logger.info("Connected to Google Sheets API")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheets: {e}", exc_info=True)
            self._client = None

    def _is_cache_valid(self) -> bool:
        return (
            len(self._cache) > 0
            and (time.time() - self._cache_time) < settings.SHEETS_CACHE_TTL
        )

    async def _fetch_all_records(self) -> list[dict]:
        """Fetch all records from the sheet, using cache if valid."""
        if self._is_cache_valid():
            return self._cache

        if not self._client:
            logger.warning("Sheets client not connected, attempting reconnect")
            await self.connect()
            if not self._client:
                return []

        try:
            sheet = self._client.open_by_key(settings.GOOGLE_SHEETS_ID).worksheet("Reparaciones")
            records = sheet.get_all_records()
            self._cache = records
            self._cache_time = time.time()
            logger.info(f"Fetched {len(records)} records from Google Sheets")
            return records
        except Exception as e:
            logger.error(f"Error fetching sheet data: {e}", exc_info=True)
            if self._cache:
                logger.warning("Returning stale cache due to fetch error")
                return self._cache
            return []

    async def get_repairs_by_phone(self, phone: str) -> list[dict]:
        """Get all repairs for a given phone number."""
        records = await self._fetch_all_records()

        matches = []
        for record in records:
            record_phone = str(record.get("cliente_telefono", ""))
            if record_phone and phones_match(record_phone, phone):
                matches.append(_extract_repair(record))

        logger.info(f"Found {len(matches)} repairs for phone {phone}")
        return matches

    async def get_repair_by_resguardo(self, resguardo: str, phone: str) -> dict | None:
        """
        Get a specific repair by resguardo number.
        Returns the repair only if it belongs to the given phone number.
        Returns None if not found or phone doesn't match.
        """
        records = await self._fetch_all_records()
        resguardo_clean = resguardo.strip()

        for record in records:
            if str(record.get("resguardo", "")).strip() == resguardo_clean:
                record_phone = str(record.get("cliente_telefono", ""))
                if record_phone and phones_match(record_phone, phone):
                    return _extract_repair(record)
                else:
                    # Resguardo exists but belongs to another client
                    return None

        return None

    async def _fetch_all_prices(self) -> list[dict]:
        """Fetch all records from the Prices sheet, using cache if valid."""
        if (
            len(self._prices_cache) > 0
            and (time.time() - self._prices_cache_time) < settings.SHEETS_CACHE_TTL
        ):
            return self._prices_cache

        if not self._client:
            await self.connect()
            if not self._client:
                return []

        try:
            sheet = self._client.open_by_key(settings.GOOGLE_PRICES_SHEET_ID).worksheet("Precios")
            # Header row is row 2 (row 1 is title)
            all_values = sheet.get_all_values()
            headers = all_values[1]  # Row 2 = index 1
            records = []
            for row in all_values[3:]:
                if any(cell.strip() for cell in row):
                    record = dict(zip(headers, row))
                    records.append(record)
            self._prices_cache = records
            self._prices_cache_time = time.time()
            logger.info(f"Fetched {len(records)} price records from Google Sheets")
            return records
        except Exception as e:
            logger.error(f"Error fetching prices sheet: {e}", exc_info=True)
            if self._prices_cache:
                return self._prices_cache
            return []

    async def get_all_prices(self) -> list[dict]:
        """Get all price records."""
        records = await self._fetch_all_prices()
        prices = []
        for r in records:
            prices.append({
                "categoria": str(r.get("Categoria", "")).strip(),
                "marca": str(r.get("Marca", "")).strip(),
                "modelo": str(r.get("Modelo", "")).strip(),
                "tipo_reparacion": str(r.get("Tipo_Reparacion", "")).strip(),
                "precio": str(r.get("Precio (S/)", "")).strip(),
                "disponible": str(r.get("Disponible", "")).strip(),
            })
        return prices

    def format_prices_for_prompt(self, prices: list[dict]) -> str:
        """Format price data into context for the AI."""
        if not prices:
            return ""

        lines = ["[TABLA DE PRECIOS DE REPARACIONES]"]
        lines.append(f"Total de servicios disponibles: {len(prices)}\n")

        for p in prices:
            disponible = "DISPONIBLE" if p["disponible"].lower() == "si" else "NO DISPONIBLE"
            lines.append(
                f"- {p['marca']} {p['modelo']} | {p['tipo_reparacion']} | "
                f"{p['precio']} | {disponible}"
            )

        lines.append("\nINSTRUCCIONES PRECIOS:")
        lines.append("- Si el cliente pregunta por un precio, busca la coincidencia más cercana por marca, modelo y tipo de reparación.")
        lines.append("- Muestra el precio siempre.")
        lines.append("- Si el servicio tiene 'NO DISPONIBLE', informa el precio pero aclara que actualmente NO está disponible.")
        lines.append("- Si no encuentras el servicio exacto, muestra los servicios disponibles para ese modelo/marca.")
        lines.append("- NUNCA inventes precios que no estén en esta tabla.")

        return "\n".join(lines)

    def format_repairs_for_prompt(self, repairs: list[dict]) -> str:
        """Format repair data into context for the AI, separating active from closed."""
        if not repairs:
            return ""

        active = [r for r in repairs if _is_active(r)]
        closed = [r for r in repairs if not _is_active(r)]

        lines = ["[DATOS DE REPARACIÓN DEL CLIENTE]"]

        # Active repairs - full detail
        if active:
            lines.append(f"\nREPARACIONES ACTIVAS ({len(active)}):")
            lines.append("Estos equipos aún están en proceso o pendientes de recogida/envío.\n")
            for i, r in enumerate(active, 1):
                lines.append(f"--- Equipo {i} de {len(active)} ---")
                lines.extend(_format_single_repair(r))
                lines.append("")
        else:
            lines.append("\nNo tiene reparaciones activas en este momento.")

        # Closed repairs - minimal summary
        if closed:
            lines.append(f"\nREPARACIONES ANTERIORES FINALIZADAS: {len(closed)}")
            lines.append("Detalle disponible solo si el cliente pregunta por un resguardo concreto.\n")
            for r in closed:
                estado_entrega = r.get("estado_entrega", "")
                lines.append(
                    f"  Resguardo {r.get('resguardo', '?')} | "
                    f"{r.get('equipo_modelo', '?')} | "
                    f"{estado_entrega}"
                )
            lines.append("")

        # Instructions for GPT
        lines.append("INSTRUCCIONES:")
        lines.append("- Si el cliente pregunta en general, responde SOLO sobre las reparaciones ACTIVAS.")
        lines.append("- Si tiene varios equipos activos, informa de TODOS, no solo del primero.")
        lines.append("- Si no tiene activas pero si anteriores, informa: 'No tienes reparaciones activas. Tienes X reparaciones anteriores ya finalizadas.'")
        lines.append("- Si pregunta por un resguardo específico del historial, indícale su estado.")
        lines.append("- NUNCA inventes información que no esté en estos datos.")

        return "\n".join(lines)


def _format_single_repair(r: dict) -> list[str]:
    """Format a single repair into readable lines."""
    lines = [
        f"Nº Resguardo: {r.get('resguardo', 'N/A')}",
        f"Equipo: {r.get('equipo_modelo', 'N/A')}",
        f"Problema: {r.get('sintoma', 'N/A')}",
        f"Estado: {r.get('estado', 'N/A')}",
        f"Recibido: {r.get('fecha_recepcion', 'N/A')}",
    ]
    if r.get("presupuesto_aceptado_id"):
        lines.append(f"Presupuesto: {r['presupuesto_aceptado_id']}")
    if r.get("tecnico_asignado"):
        lines.append(f"Técnico asignado: {r['tecnico_asignado']}")
    if r.get("fecha_reparacion"):
        lines.append(f"Fecha reparación: {r['fecha_reparacion']}")
    if r.get("resultado_reparacion"):
        lines.append(f"Resultado: {r['resultado_reparacion']}")
    if r.get("estado_entrega"):
        estado_e = r["estado_entrega"]
        if estado_e == "ENVIO":
            lines.append("Estado entrega: EN CAMINO (enviado al cliente)")
        else:
            lines.append(f"Estado entrega: {estado_e}")
    return lines
