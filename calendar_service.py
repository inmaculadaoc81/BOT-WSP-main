import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MADRID_TZ = ZoneInfo("Europe/Madrid")


class CalendarService:
    """Google Calendar integration."""

    def __init__(self):
        self.calendar_id = settings.GOOGLE_CALENDAR_ID
        self.subject = getattr(settings, "GOOGLE_CALENDAR_SUBJECT", None)
        self._service = None

    def _get_service(self):
        """Build the Calendar API service."""
        if self._service:
            return self._service

        creds = Credentials.from_service_account_file(
            settings.GOOGLE_CREDENTIALS_PATH,
            scopes=SCOPES,
        )

        # Solo usar impersonation si realmente tienes domain-wide delegation configurado
        if self.subject:
            try:
                creds = creds.with_subject(self.subject)
            except Exception:
                logger.warning(
                    "No se pudo aplicar with_subject; se usará la service account sin impersonation.",
                    exc_info=True,
                )

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

        try:
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            end_dt = start_dt + timedelta(minutes=duration_minutes)

            full_description = description.strip()
            if attendee_phone:
                full_description = f"{full_description}\nTeléfono: {attendee_phone}".strip()

            event_body = {
                "summary": title,
                "description": full_description,
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": "Europe/Madrid",
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": "Europe/Madrid",
                },
            }

            logger.info(
                "Creating calendar event",
                extra={
                    "calendar_id": self.calendar_id,
                    "title": title,
                    "start_iso": start_dt.isoformat(),
                },
            )

            event = (
                service.events()
                .insert(
                    calendarId=self.calendar_id,
                    body=event_body,
                )
                .execute()
            )

            logger.info(
                "Calendar event created successfully",
                extra={
                    "event_id": event.get("id"),
                    "htmlLink": event.get("htmlLink"),
                },
            )
            return event

        except Exception as e:
            logger.exception(f"Error creating calendar event: {e}")
            return None

    def get_appointment_context(self) -> str:
        """Return appointment instructions as context for the AI."""
        now = datetime.now(MADRID_TZ)
        today = now.strftime("%A %d/%m/%Y")

        return (
            f"[SISTEMA DE CITAS Y ENVIOS]\n"
            f"Fecha actual: {today}\n"
            f"Horario de atencion del local: Lunes a Viernes de 09:30 a 18:00.\n"
            f"Sabados, domingos y festivos cerrados.\n"
            f"\nHay DOS tipos de agenda:\n"
            f"1. CITA: el cliente viene al local.\n"
            f"2. ENVIO: se envia un mensajero a recoger el equipo al domicilio del cliente (coste: 15€ por equipo, solo peninsula).\n"
            f"\nComo distinguirlos:\n"
            f"- CITA: el cliente dice que quiere ir al local, pasar, llevar el equipo, agendar cita, visita.\n"
            f"- ENVIO: el cliente dice recogida a domicilio, que lo recojan, no puede ir, quiere enviar el equipo, que pasen a buscarlo.\n"
            f"- Si no queda claro, pregunta: 'Prefieres traer el equipo a nuestro local o que enviemos un mensajero a recogerlo a domicilio (15€ por equipo)?'\n"
            f"\nPROTOCOLO DE CITA (cliente viene al local):\n"
            f"1. Datos necesarios: nombre completo + correo electronico + numero de telefono + motivo (equipo + problema).\n"
            f"2. Si falta alguno, pidelo antes de continuar.\n"
            f"3. Pregunta fecha y hora preferida.\n"
            f"4. Las citas de diagnostico solo se pueden agendar de Lunes a Viernes entre 10:00 y 17:00.\n"
            f"5. Si pide horario fuera de rango (antes de 10:00, despues de 17:00, fin de semana o festivo), indicale el horario correcto.\n"
            f"6. Cuando tengas TODOS los datos, muestra un RESUMEN para que el cliente confirme:\n"
            f"   '📋 *Resumen de tu cita:*\n"
            f"   👤 Nombre: [nombre]\n"
            f"   🔧 Motivo: [equipo + problema]\n"
            f"   📅 Fecha: [fecha y hora]\n"
            f"   📍 Lugar: C/ Joaquin Maria Lopez 26, Madrid\n"
            f"   ¿Es correcto?'\n"
            f"7. SOLO cuando el cliente responda confirmando (si, correcto, ok, perfecto, dale, etc.), genera en una linea separada al final:\n"
            f"CONFIRMAR_CITA|<datetime_iso>|<nombre_cliente>|<motivo>\n"
            f"Ejemplo: CONFIRMAR_CITA|2026-03-27T10:00:00+01:00|Juan Garcia|Reparacion portatil HP\n"
            f"8. Tras confirmar, NO digas que ya esta registrada. Di: 'Perfecto 😊 Estoy gestionando tu cita. En cuanto quede registrada, te enviaremos la confirmacion final.'\n"
            f"\nPROTOCOLO DE ENVIO (mensajero recoge a domicilio):\n"
            f"1. Datos necesarios: nombre completo + correo electronico + numero de telefono + motivo (equipo + problema) + direccion completa (calle, numero, CP, ciudad).\n"
            f"2. Si falta alguno, pidelo antes de continuar.\n"
            f"3. Solo se ofrece recogida para Thermomix, Dyson y portatiles. No para torres, all in one ni robot aspiradores.\n"
            f"4. Informar del coste: 15€ por equipo.\n"
            f"5. Para recogida, solo pedir el DIA preferido. No confirmar hora exacta.\n"
            f"6. Si la solicitud se hace despues de las 13:00, solo puede programarse a partir del dia subsiguiente.\n"
            f"7. Cuando tengas TODOS los datos, muestra un RESUMEN para que el cliente confirme:\n"
            f"   '📋 *Resumen de tu recogida:*\n"
            f"   👤 Nombre: [nombre]\n"
            f"   🔧 Motivo: [equipo + problema]\n"
            f"   📍 Direccion: [direccion completa]\n"
            f"   📅 Fecha: [fecha]\n"
            f"   💰 Coste: 15€\n"
            f"   ¿Es correcto?'\n"
            f"8. SOLO cuando el cliente responda confirmando (si, correcto, ok, perfecto, dale, etc.), genera en una linea separada al final:\n"
            f"CONFIRMAR_ENVIO|<datetime_iso>|<nombre_cliente>|<motivo>|<direccion>\n"
            f"Ejemplo: CONFIRMAR_ENVIO|2026-03-31T10:00:00+01:00|Maria Lopez|Dyson V15 no aspira|Calle Gran Via 10 2B, 28013 Madrid\n"
            f"9. Tras confirmar, NO digas que ya esta registrada. Di: 'Perfecto 😊 Estoy gestionando tu solicitud de recogida. Un tecnico se pondra en contacto contigo para confirmarla.'\n"
            f"\nIMPORTANTE:\n"
            f"- NUNCA generes CONFIRMAR_CITA ni CONFIRMAR_ENVIO sin mostrar primero el resumen y recibir confirmacion explicita del cliente.\n"
            f"- Si el cliente dice que algun dato es incorrecto, corrigelo y muestra el resumen de nuevo.\n"
            f"- Si el cliente NO ha confirmado, NO incluyas ninguna linea CONFIRMAR.\n"
            f"- No repitas el saludo inicial si ya estabas en la misma conversacion.\n"
            f"- Fuera de horario puedes seguir respondiendo consultas informativas; solo aclara el horario si el cliente quiere ir, entregar, recoger o agendar."
        )


def extract_confirmation_command(ai_response: str) -> dict | None:
    """
    Busca una línea CONFIRMAR_CITA|... o CONFIRMAR_ENVIO|...
    y devuelve un dict con los datos.
    """
    if not ai_response:
        return None

    lines = [line.strip() for line in ai_response.splitlines() if line.strip()]

    for line in lines:
        if line.startswith("CONFIRMAR_CITA|"):
            parts = line.split("|", 3)
            if len(parts) != 4:
                logger.warning("Formato inválido en CONFIRMAR_CITA", extra={"line": line})
                return None

            return {
                "type": "cita",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "reason": parts[3].strip(),
                "raw_line": line,
            }

        if line.startswith("CONFIRMAR_ENVIO|"):
            parts = line.split("|", 4)
            if len(parts) != 5:
                logger.warning("Formato inválido en CONFIRMAR_ENVIO", extra={"line": line})
                return None

            return {
                "type": "envio",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "reason": parts[3].strip(),
                "address": parts[4].strip(),
                "raw_line": line,
            }

    return None


def strip_confirmation_command(ai_response: str) -> str:
    """
    Elimina la línea CONFIRMAR_* del texto para no enviársela al usuario.
    """
    if not ai_response:
        return ai_response

    clean_lines = []
    for line in ai_response.splitlines():
        stripped = line.strip()
        if stripped.startswith("CONFIRMAR_CITA|") or stripped.startswith("CONFIRMAR_ENVIO|"):
            continue
        clean_lines.append(line)

    return "\n".join(clean_lines).strip()


async def process_ai_calendar_command(
    calendar_service: CalendarService,
    ai_response: str,
    attendee_phone: str = "",
) -> tuple[str, dict | None]:
    """
    1. Limpia el mensaje para el usuario
    2. Detecta si hay comando CONFIRMAR_*
    3. Crea el evento real en Google Calendar
    4. Devuelve:
       - user_message: texto limpio para enviar al cliente
       - created_event: evento creado o None
    """
    user_message = strip_confirmation_command(ai_response)
    command = extract_confirmation_command(ai_response)

    if not command:
        return user_message, None

    created_event = None

    if command["type"] == "cita":
        created_event = await calendar_service.create_event(
            title=f"CITA: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=command["reason"],
            attendee_phone=attendee_phone,
        )

        if created_event:
            start_dt = datetime.fromisoformat(command["datetime_iso"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            pretty_date = start_dt.astimezone(MADRID_TZ).strftime("%d/%m/%Y")
            pretty_time = start_dt.astimezone(MADRID_TZ).strftime("%H:%M")

            user_message = (
                f"✅ Tu cita ha sido registrada para el {pretty_date} a las {pretty_time}.\n"
                f"Te esperamos en C/ Joaquín María López 26, Madrid."
            )
        else:
            user_message = (
                "Perfecto 😊 He recibido tu solicitud de cita, pero ahora mismo no he podido registrarla automáticamente.\n"
                "Un técnico la revisará y te confirmará en breve."
            )

    elif command["type"] == "envio":
        created_event = await calendar_service.create_event(
            title=f"ENVIO: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=f"{command['reason']}\nDirección: {command['address']}",
            attendee_phone=attendee_phone,
        )

        if created_event:
            start_dt = datetime.fromisoformat(command["datetime_iso"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            pretty_date = start_dt.astimezone(MADRID_TZ).strftime("%d/%m/%Y")

            user_message = (
                f"✅ Tu recogida ha sido registrada para el {pretty_date}.\n"
                f"El mensajero pasará por {command['address']}.\n"
                f"Coste: 15€ por equipo.\n\n"
                f"Un técnico se pondrá en contacto contigo para confirmar la recogida."
            )
        else:
            user_message = (
                "Perfecto 😊 He recibido tu solicitud de recogida, pero ahora mismo no he podido registrarla automáticamente.\n"
                "Un técnico se pondrá en contacto contigo para confirmarla."
            )

    return user_message, created_event