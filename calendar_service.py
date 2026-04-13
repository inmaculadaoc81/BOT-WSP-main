import logging
from datetime import datetime, timedelta, timezone, time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Madrid timezone offset (CET=+1, CEST=+2). Using fixed offset for simplicity.
MADRID_TZ = timezone(timedelta(hours=2))  # CEST (summer), adjust if needed


class CalendarService:
    """Google Calendar integration via domain-wide delegation."""

    def __init__(self):
        self.calendar_id = settings.GOOGLE_CALENDAR_ID
        self.subject = settings.GOOGLE_CALENDAR_SUBJECT
        self._service = None

    def _get_service(self):
        """Build the Calendar API service with impersonation."""
        if self._service:
            return self._service

        creds = Credentials.from_service_account_file(
            settings.GOOGLE_CREDENTIALS_PATH,
            scopes=SCOPES,
        )
        creds = creds.with_subject(self.subject)
        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    async def create_event(
        self,
        title: str,
        start_iso: str,
        duration_minutes: int = 30,
        description: str = "",
        attendee_phone: str = "",
    ) -> dict | None:
        """Create a calendar event."""
        service = self._get_service()

        start_dt = datetime.fromisoformat(start_iso)
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        event_body = {
            "summary": title,
            "description": f"{description}\nTeléfono: {attendee_phone}".strip(),
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "Europe/Madrid",
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "Europe/Madrid",
            },
        }

        try:
            event = service.events().insert(
                calendarId=self.calendar_id,
                body=event_body,
            ).execute()
            logger.info(f"Calendar event created: {event.get('id')} at {start_iso}")
            return event
        except Exception as e:
            logger.error(f"Error creating calendar event: {e}", exc_info=True)
            return None

    def get_appointment_context(self) -> str:
        """Return appointment instructions as context for the AI."""
        now = datetime.now(MADRID_TZ)
        today = now.strftime("%A %d/%m/%Y")

        return (
            f"[SISTEMA DE CITAS Y ENVIOS]\n"
            f"Fecha actual: {today}\n"
            f"Horario de atencion: Lunes a Viernes de 09:30 a 18:00.\n"
            f"Sabados, domingos y festivos cerrados.\n"
            f"\nHay DOS tipos de agenda:\n"
            f"1. CITA: el cliente viene al local.\n"
            f"2. ENVIO: se envia un mensajero a recoger el equipo al domicilio del cliente (coste: 15€ por equipo, solo peninsula).\n"
            f"\nComo distinguirlos:\n"
            f"- CITA: el cliente dice que quiere ir al local, pasar, llevar el equipo, agendar cita, visita.\n"
            f"- ENVIO: el cliente dice recogida a domicilio, que lo recojan, no puede ir, quiere enviar el equipo, que pasen a buscarlo.\n"
            f"- Si no queda claro, pregunta: 'Prefieres traer el equipo a nuestro local o que enviemos un mensajero a recogerlo a domicilio (15€ por equipo)?'\n"
            f"\nPROTOCOLO DE CITA (cliente viene al local):\n"
            f"1. Datos necesarios: nombre completo + motivo (equipo + problema).\n"
            f"2. Si falta alguno, pidelo antes de continuar.\n"
            f"3. Pregunta fecha y hora preferida (dentro del horario).\n"
            f"4. Si pide horario fuera de rango (antes de 09:30, despues de 18:00, fin de semana o festivo), indicale el horario correcto.\n"
            f"5. Cuando tengas TODOS los datos, muestra un RESUMEN para que el cliente confirme:\n"
            f"   '📋 *Resumen de tu cita:*\n"
            f"   👤 Nombre: [nombre]\n"
            f"   🔧 Motivo: [equipo + problema]\n"
            f"   📅 Fecha: [fecha y hora]\n"
            f"   📍 Lugar: C/ Joaquin Maria Lopez 26, Madrid\n"
            f"   ¿Es correcto?'\n"
            f"6. SOLO cuando el cliente responda confirmando (si, correcto, ok, perfecto, dale, etc.), genera en una linea separada al final:\n"
            f"CONFIRMAR_CITA|<datetime_iso>|<nombre_cliente>|<motivo>\n"
            f"Ejemplo: CONFIRMAR_CITA|2026-03-27T10:00:00+02:00|Juan Garcia|Reparacion portatil HP\n"
            f"7. Tras confirmar, di: '✅ Tu cita ha sido registrada para [fecha] a las [hora]. Te esperamos en C/ Joaquin Maria Lopez 26, Madrid.'\n"
            f"\nPROTOCOLO DE ENVIO (mensajero recoge a domicilio):\n"
            f"1. Datos necesarios: nombre completo + motivo (equipo + problema) + direccion completa (calle, numero, CP, ciudad).\n"
            f"2. Si falta alguno, pidelo antes de continuar.\n"
            f"3. Informar del coste: 15€ por equipo.\n"
            f"4. Pregunta fecha preferida de recogida (dentro del horario, L-V).\n"
            f"5. Cuando tengas TODOS los datos, muestra un RESUMEN para que el cliente confirme:\n"
            f"   '📋 *Resumen de tu recogida:*\n"
            f"   👤 Nombre: [nombre]\n"
            f"   🔧 Motivo: [equipo + problema]\n"
            f"   📍 Direccion: [direccion completa]\n"
            f"   📅 Fecha: [fecha]\n"
            f"   💰 Coste: 15€\n"
            f"   ¿Es correcto?'\n"
            f"6. SOLO cuando el cliente responda confirmando (si, correcto, ok, perfecto, dale, etc.), genera en una linea separada al final:\n"
            f"CONFIRMAR_ENVIO|<datetime_iso>|<nombre_cliente>|<motivo>|<direccion>\n"
            f"Ejemplo: CONFIRMAR_ENVIO|2026-03-31T10:00:00+02:00|Maria Lopez|Dyson V15 no aspira|Calle Gran Via 10 2B, 28013 Madrid\n"
            f"7. Tras confirmar, di: '✅ Tu recogida ha sido registrada para [fecha]. El mensajero pasara por [direccion]. Coste: 15€ por equipo.'\n"
            f"\nIMPORTANTE:\n"
            f"- NUNCA generes CONFIRMAR_CITA ni CONFIRMAR_ENVIO sin mostrar primero el resumen y recibir confirmacion explicita del cliente.\n"
            f"- Si el cliente dice que algun dato es incorrecto, corrigelo y muestra el resumen de nuevo.\n"
            f"- Si el cliente NO ha confirmado, NO incluyas ninguna linea CONFIRMAR."
        )
