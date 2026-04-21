import logging
import time
import re

import gspread
from google.oauth2.service_account import Credentials

from config import settings

logger = logging.getLogger(__name__)

# Columnas seguras que SE EXPONEN al modelo (sin datos sensibles de coste/proveedor).
# Cualquier columna fuera de esta lista se ignora al formatear para el prompt.
REPAIR_COLUMNS = [
    "resguardo",
    "fecha_recepcion",
    "cliente_nombre",
    "equipo_modelo",
    "sintoma",
    "estado",
    "tecnico_asignado",
    "fecha_reparacion",
    "fecha_presupuesto",
    "motivo_rechazo",
    "fecha_aceptacion_presupuesto",
    "fecha_entrega",
    "fecha_recogida",
    "estado_entrega",
    "tiempo_entrega",
]

# Mapeo de cabeceras humanas (hoja "Consolidado") a nombres internos snake_case.
# Se aceptan variantes con/sin tildes y con espacios alternativos.
# Columnas NO mapeadas se ignoran silenciosamente (filtradas por REPAIR_COLUMNS).
HEADER_ALIASES = {
    # Identificadores básicos
    "Resguardo de Recepcion": "resguardo",
    "Resguardo de Recepción": "resguardo",
    "Fecha": "fecha_recepcion",
    "Fecha de Recepcion": "fecha_recepcion",
    "Fecha de Recepción": "fecha_recepcion",

    # Cliente
    "Nombre de Cliente": "cliente_nombre",
    "Nombre del Cliente": "cliente_nombre",
    "Telefono": "cliente_telefono",
    "Teléfono": "cliente_telefono",
    "Correo electrónico": "cliente_correo",
    "Correo electronico": "cliente_correo",

    # Equipo
    "Modelo/Marca Equipo": "equipo_modelo",
    "Modelo / Marca Equipo": "equipo_modelo",
    "Síntoma / Reparación": "sintoma",
    "Sintoma / Reparacion": "sintoma",
    " MODELO DYSON/THERMOMIX": "modelo_especifico",
    "MODELO DYSON/THERMOMIX": "modelo_especifico",
    "TIPO DE REPARACIÓN DYSON/THERMOMIX": "tipo_reparacion_marca",
    "TIPO DE REPARACION DYSON/THERMOMIX": "tipo_reparacion_marca",

    # Estado y seguimiento
    "Estado": "estado",
    "Estado de Pedido": "estado_pedido_interno",
    "ESTADO DE RECOGIDA": "estado_entrega",
    "Aviso Wasap Estado": "aviso_wasap",

    # Presupuesto
    "Responsable de presupuesto": "responsable_presupuesto",
    "Fecha de Elaboración de Presupuesto": "fecha_presupuesto",
    "Fecha de Elaboracion de Presupuesto": "fecha_presupuesto",
    "Fecha Límite Presupuesto": "fecha_limite_presupuesto",
    "Fecha Limite Presupuesto": "fecha_limite_presupuesto",
    "Alerta envío de presupuesto": "alerta_presupuesto",
    "Alerta envio de presupuesto": "alerta_presupuesto",
    "Motivo Rechazo de Presupuesto": "motivo_rechazo",
    "FECHA ACEPTACION DE PRESUPUESTO (FECHA DE PEDIDO)": "fecha_aceptacion_presupuesto",

    # Reparación y entrega
    "Técnico que ha reparado el equipo": "tecnico_asignado",
    "Tecnico que ha reparado el equipo": "tecnico_asignado",
    "Fecha de Reparación": "fecha_reparacion",
    "Fecha de Reparacion": "fecha_reparacion",
    "TIEMPO (DÍAS) DE ENTREGA DE EQUIPO": "tiempo_entrega",
    "TIEMPO (DIAS) DE ENTREGA DE EQUIPO": "tiempo_entrega",
    "FECHA DE ENTREGA": "fecha_entrega",
    "FECHA DE RECOGIDA POR EL CLIENTE": "fecha_recogida",

    # Internas / sensibles → mapeadas pero filtradas por REPAIR_COLUMNS
    "Costo de Reparación sin IVA (No incluir precio de la pieza)": "costo_reparacion_interno",
    "Costo de Reparacion sin IVA (No incluir precio de la pieza)": "costo_reparacion_interno",
    "COSTO DE PIEZA": "costo_pieza_interno",
    "Ganancia Neta": "ganancia_neta_interna",
    "Responsable de Compra": "responsable_compra_interno",
    "PROVEEDOR": "proveedor_interno",
    "ENLACES DE COMPRA": "enlaces_compra_interno",
    "NÚMERO DE PEDIDO DE COMPRA": "numero_pedido_interno",
    "NUMERO DE PEDIDO DE COMPRA": "numero_pedido_interno",
    "FECHA DE PEDIDO": "fecha_pedido_interno",
    "CONTACTAR PROVEEDOR": "contactar_proveedor_interno",
    "FECHA CONTACTO 1": "fecha_contacto_1_interno",
    "RECORDATORIO P1": "recordatorio_p1_interno",
    "NÚMERO DE FACTURA": "numero_factura_interno",
    "NUMERO DE FACTURA": "numero_factura_interno",
    "FICHA /MARCA": "ficha_marca_interna",
    "FICHA/MARCA": "ficha_marca_interna",
    "Colocó Reseña": "coloco_resena_interno",
    "Coloco Reseña": "coloco_resena_interno",
    "OBSERVACIONES": "observaciones_internas",
    "Envío de Encuesta": "envio_encuesta_interno",
    "Envio de Encuesta": "envio_encuesta_interno",
    "Envío de enlace para reseña": "envio_enlace_resena_interno",
    "Envio de enlace para reseña": "envio_enlace_resena_interno",
    "Ingresó Reseña?": "ingreso_resena_interno",
    "Ingreso Reseña?": "ingreso_resena_interno",
    "Obs (Entrega de Equipos)": "obs_entrega_interno",
}


def _normalize_headers(record: dict) -> dict:
    """Translate human headers to snake_case internal keys (HEADER_ALIASES).
    Keys not in the map are kept as-is so existing sheets with snake_case keep working."""
    normalized = {}
    for key, value in record.items():
        canonical = HEADER_ALIASES.get(key, key)
        normalized[canonical] = value
    return normalized


# Estados que significan que la reparacion ya esta cerrada para el cliente.
# Se comparan en mayusculas. Si algun dia se introducen estados nuevos de cierre,
# anadirlos aqui.
CLOSED_STATES = {
    "ENTREGADO",
    "RECICLAJE",
    "REPARADO Y ENTREGADO",
    "ANULADO",
}


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
    """A repair is active unless its delivery/estado indicates closure.
    Si la hoja tiene estado_entrega, manda ese. Si no, caemos a `estado`."""
    estado_entrega = (repair.get("estado_entrega") or "").upper().strip()
    if estado_entrega:
        return estado_entrega not in CLOSED_STATES
    estado = (repair.get("estado") or "").upper().strip()
    return estado not in CLOSED_STATES


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
            sheet = self._client.open_by_key(settings.GOOGLE_SHEETS_ID).worksheet("Consolidado")
            raw_records = sheet.get_all_records()
            records = [_normalize_headers(r) for r in raw_records]
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

    async def get_repair_by_resguardo(self, resguardo: str, phone: str | None = None) -> dict | None:
        """Get a specific repair by resguardo number.

        Si `phone` es None, no se valida la propiedad del resguardo y se
        devuelve la reparacion tal cual. Si `phone` se pasa, se exige que el
        telefono coincida (modo seguro legado).
        """
        records = await self._fetch_all_records()
        resguardo_clean = resguardo.strip()

        for record in records:
            if str(record.get("resguardo", "")).strip() == resguardo_clean:
                if phone is None:
                    return _extract_repair(record)
                record_phone = str(record.get("cliente_telefono", ""))
                if record_phone and phones_match(record_phone, phone):
                    return _extract_repair(record)
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
    if r.get("fecha_presupuesto"):
        lines.append(f"Presupuesto elaborado: {r['fecha_presupuesto']}")
    if r.get("fecha_aceptacion_presupuesto"):
        lines.append(f"Presupuesto aceptado: {r['fecha_aceptacion_presupuesto']}")
    if r.get("motivo_rechazo"):
        lines.append(f"Motivo rechazo presupuesto: {r['motivo_rechazo']}")
    if r.get("tecnico_asignado"):
        lines.append(f"Técnico asignado: {r['tecnico_asignado']}")
    if r.get("fecha_reparacion"):
        lines.append(f"Fecha reparación: {r['fecha_reparacion']}")
    if r.get("tiempo_entrega"):
        lines.append(f"Tiempo estimado de entrega (días): {r['tiempo_entrega']}")
    if r.get("fecha_entrega"):
        lines.append(f"Fecha entrega al cliente: {r['fecha_entrega']}")
    if r.get("fecha_recogida"):
        lines.append(f"Fecha de recogida por el cliente: {r['fecha_recogida']}")
    if r.get("estado_entrega"):
        estado_e = r["estado_entrega"]
        if estado_e.upper() == "ENVIO":
            lines.append("Estado entrega: EN CAMINO (enviado al cliente)")
        else:
            lines.append(f"Estado entrega: {estado_e}")
    return lines
