import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MADRID_TZ = ZoneInfo("Europe/Madrid")

# Validaciones de datos para citas/recogidas (sanity checks que el LLM puede saltarse).
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_PLACEHOLDER_NAMES = {"cliente", "anonimo", "anónimo", "sin nombre", "n/a", "test", "prueba"}


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

    def get_busy_slots_for_day(self, datetime_iso: str) -> list[tuple]:
        """Return list of (start, end) datetime pairs for all events on the given day."""
        try:
            dt = datetime.fromisoformat(datetime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
        except (ValueError, TypeError):
            return []

        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        try:
            service = self._get_service()
            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            busy = []
            for event in result.get("items", []):
                s_str = event.get("start", {}).get("dateTime")
                e_str = event.get("end", {}).get("dateTime")
                if s_str and e_str:
                    s = datetime.fromisoformat(s_str).astimezone(MADRID_TZ)
                    e = datetime.fromisoformat(e_str).astimezone(MADRID_TZ)
                    busy.append((s, e))
            return busy
        except Exception as exc:
            logger.error(f"Error fetching busy slots: {exc}", exc_info=True)
            return []

    def get_available_slots(self, datetime_iso: str, duration_minutes: int = 30) -> list[str]:
        """Return free HH:MM slots within 10:00-17:00 for the given day."""
        busy = self.get_busy_slots_for_day(datetime_iso)

        try:
            dt = datetime.fromisoformat(datetime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
        except (ValueError, TypeError):
            return []

        free = []
        current = local.replace(hour=10, minute=0, second=0, microsecond=0)
        cutoff = local.replace(hour=17, minute=0, second=0, microsecond=0)

        while current <= cutoff:
            slot_end = current + timedelta(minutes=duration_minutes)
            if not _overlaps(current, slot_end, busy):
                free.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)

        return free

    def get_appointment_context(self) -> str:
        """Return appointment instructions as context for the AI."""
        now = datetime.now(MADRID_TZ)
        today = now.strftime("%A %d/%m/%Y")

        return (
            f"[SISTEMA DE CITAS Y ENVIOS]\n"
            f"Fecha actual: {today}\n"
            f"Horario de atencion del local: Lunes a Viernes de 09:30 a 18:00.\n"
            f"Sabados, domingos y festivos cerrados.\n"
            f"\n🚨 DIFERENCIA CRITICA — LEE ANTES DE ACTUAR:\n"
            f"\nHay TRES situaciones distintas. NO las confundas:\n"
            f"\n1. WALK-IN (DEFAULT — el cliente solo quiere venir al local sin agendar):\n"
            f"   Senales: 'voy a llevar', 'me acerco al local', 'paso por la tienda', 'voy mañana',\n"
            f"   'puedo ir hoy?', '¿que horario tienen?', '¿donde estais?', '¿hay parking?'.\n"
            f"   ❌ NO pidas NINGUN dato (ni nombre ni email ni telefono ni cita).\n"
            f"   ✅ Solo da: direccion (C/ Joaquin Maria Lopez 26, Madrid), horario (L-V 09:30-18:00), parking.\n"
            f"   ✅ Recuerdale que puede pasar SIN CITA PREVIA dentro del horario.\n"
            f"   ✅ NO ofrezcas agendar cita salvo que el cliente lo pida explicitamente.\n"
            f"\n2. CITA EN EL LOCAL (solo si el cliente DICE LA PALABRA 'cita', 'agendar', 'reservar', 'programar'):\n"
            f"   Solo entonces entras en el flujo de pedir nombre+email+telefono+motivo+dia+hora.\n"
            f"\n3. RECOGIDA A DOMICILIO (mensajero) — solo si el cliente pide explicitamente que pasen a recoger:\n"
            f"   Senales: 'recogida', 'que lo recojan', 'mensajero', 'no puedo ir', 'quiero enviar', 'pasen a buscarlo'.\n"
            f"   Coste: 15€ por equipo, solo peninsula.\n"
            f"\n⚠️ REGLA ANTI-AGENDAMIENTO ACCIDENTAL:\n"
            f"- Si el cliente solo respondio 'si' a algo que TU le preguntaste y NO se ve en la conversacion una peticion explicita previa de cita/recogida del CLIENTE, NUNCA registres cita ni emitas CONFIRMAR_*.\n"
            f"- Si dudas si quiere walk-in o cita, pregunta de forma neutra: '¿Prefieres pasar directamente al local sin cita o agendar una cita con un tecnico?'\n"
            f"\nPROTOCOLO DE CITA (cliente viene al local):\n"
            f"1. Datos necesarios: nombre completo + correo electronico + numero de telefono + motivo (equipo + problema).\n"
            f"2. Si falta alguno, pidelo antes de continuar.\n"
            f"3. Pregunta fecha y hora preferida.\n"
            f"4. Las citas de diagnostico solo se pueden agendar de Lunes a Viernes entre 10:00 y 17:00 (ultimo slot a las 17:00).\n"
            f"5. Si pide horario fuera de rango (antes de 10:00, despues de 17:00, fin de semana o festivo), indicale el horario correcto.\n"
            f"   IMPORTANTE: si el sistema devuelve un mensaje indicando que la hora ya esta ocupada y ofrece alternativas, trasladaselo al cliente tal cual y preguntale que hora prefiere de las disponibles.\n"
            f"6. Cuando tengas TODOS los datos, muestra un RESUMEN para que el cliente confirme:\n"
            f"   '📋 *Resumen de tu cita:*\n"
            f"   👤 Nombre: [nombre]\n"
            f"   🔧 Motivo: [equipo + problema]\n"
            f"   📅 Fecha: [fecha y hora]\n"
            f"   📍 Lugar: C/ Joaquin Maria Lopez 26, Madrid\n"
            f"   ¿Es correcto?'\n"
            f"7. CUANDO EL CLIENTE CONFIRMA (dice si, correcto, ok, perfecto, dale, vale, etc.) tu respuesta DEBE contener SIEMPRE DOS PARTES (las dos, no una o la otra):\n"
            f"   PARTE A (texto visible al cliente): 'Perfecto 😊 Estoy gestionando tu cita. En cuanto quede registrada, te enviaremos la confirmacion final.'\n"
            f"   PARTE B (linea de comando interna, en una linea aparte al final, el cliente NO la ve): CONFIRMAR_CITA|<datetime_iso>|<nombre_cliente>|<motivo>\n"
            f"   EJEMPLO completo de respuesta valida cuando el cliente dice 'si':\n"
            f"   ---\n"
            f"   Perfecto 😊 Estoy gestionando tu cita. En cuanto quede registrada, te enviaremos la confirmacion final.\n"
            f"\n"
            f"   CONFIRMAR_CITA|2026-03-27T10:00:00+01:00|Juan Garcia|Reparacion portatil HP\n"
            f"   ---\n"
            f"8. NUNCA omitir la linea CONFIRMAR_CITA al confirmar. Sin esa linea, la cita NO se registra en el sistema.\n"
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
            f"8. CUANDO EL CLIENTE CONFIRMA (dice si, correcto, ok, perfecto, dale, vale, etc.) tu respuesta DEBE contener SIEMPRE DOS PARTES (las dos, no una o la otra):\n"
            f"   PARTE A (texto visible al cliente): 'Perfecto 😊 Estoy gestionando tu solicitud de recogida. Un tecnico se pondra en contacto contigo para confirmarla.'\n"
            f"   PARTE B (linea de comando interna, en una linea aparte al final, el cliente NO la ve): CONFIRMAR_ENVIO|<datetime_iso>|<nombre_cliente>|<motivo>|<direccion>\n"
            f"   EJEMPLO completo de respuesta valida cuando el cliente dice 'si':\n"
            f"   ---\n"
            f"   Perfecto 😊 Estoy gestionando tu solicitud de recogida. Un tecnico se pondra en contacto contigo para confirmarla.\n"
            f"\n"
            f"   CONFIRMAR_ENVIO|2026-04-22T10:00:00+02:00|Carlo Gabriel|Dyson SV10 ruidos|Calle Blasco de Garay 61, 28015 Madrid\n"
            f"   ---\n"
            f"9. NUNCA omitir la linea CONFIRMAR_ENVIO al confirmar. Sin esa linea, la recogida NO se registra en el sistema.\n"
            f"\nIMPORTANTE:\n"
            f"- NUNCA generes CONFIRMAR_CITA ni CONFIRMAR_ENVIO sin mostrar primero el resumen y recibir confirmacion explicita del cliente.\n"
            f"- Si el cliente dice que algun dato es incorrecto, corrigelo y muestra el resumen de nuevo.\n"
            f"- Si el cliente NO ha confirmado, NO incluyas ninguna linea CONFIRMAR.\n"
            f"- Cuando el cliente SI confirma, es OBLIGATORIO incluir la linea CONFIRMAR_CITA o CONFIRMAR_ENVIO. La linea es un comando interno que el sistema procesa para registrar la cita/recogida en Google Calendar. Si olvidas esa linea, la cita no se registra y el cliente se queda sin reserva.\n"
            f"- La linea CONFIRMAR siempre va al final, sola en su propia linea, separada por un salto de linea del resto del mensaje.\n"
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


def _overlaps(slot_start: datetime, slot_end: datetime, busy: list[tuple]) -> bool:
    """True if [slot_start, slot_end) overlaps any (start, end) in busy."""
    for bs, be in busy:
        if slot_start < be and slot_end > bs:
            return True
    return False


def _conversation_text(history: list[dict] | None) -> str:
    """Concatena el contenido de todos los mensajes del historial."""
    if not history:
        return ""
    return "\n".join(msg.get("content", "") for msg in history if msg.get("content"))


def _is_real_name(name: str) -> bool:
    """Comprueba que un nombre sea plausible: 2+ palabras y no un placeholder."""
    name = (name or "").strip()
    if not name or len(name) < 3:
        return False
    if name.lower() in _PLACEHOLDER_NAMES:
        return False
    return len(name.split()) >= 2


def _validate_appointment(
    command: dict,
    history: list[dict] | None,
    sender_phone: str,
) -> tuple[bool, list[str]]:
    """Valida que el comando CONFIRMAR_* tenga todos los datos requeridos.

    Devuelve (is_valid, lista_de_datos_faltantes). El historial es la fuente
    de verdad para email, telefono y direccion (no se confia en lo que el LLM
    haya emitido en la linea CONFIRMAR_).
    """
    missing: list[str] = []
    history_text = _conversation_text(history)

    # Nombre: el LLM lo emite en la linea CONFIRMAR; comprobamos que sea real.
    if not _is_real_name(command.get("customer_name", "")):
        missing.append("nombre completo del cliente")

    # Motivo: tambien lo emite el LLM.
    reason = (command.get("reason") or "").strip()
    if not reason or len(reason) < 3:
        missing.append("motivo (equipo + problema)")

    # Email: tiene que estar en algun mensaje del cliente.
    if not _EMAIL_RE.search(history_text):
        missing.append("correo electronico")

    # Telefono: o ya lo tenemos del remitente o aparece en el historial.
    has_phone = False
    if sender_phone:
        digits = re.sub(r"\D", "", sender_phone)
        has_phone = len(digits) >= 9
    if not has_phone and not _PHONE_RE.search(history_text):
        missing.append("numero de telefono")

    # Fecha y hora: validar que sea futura y dentro del horario de citas.
    try:
        dt = datetime.fromisoformat(command["datetime_iso"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MADRID_TZ)
        local = dt.astimezone(MADRID_TZ)
        now = datetime.now(MADRID_TZ)

        if local < now:
            missing.append("fecha y hora futura (la indicada ya paso)")
        elif local.weekday() >= 5:
            missing.append("dia de lunes a viernes (no se atiende fines de semana)")
        elif command.get("type") == "cita":
            # Solo citas exigen ventana 10:00-17:00 estrictamente.
            if not (10 <= local.hour < 17 or (local.hour == 17 and local.minute == 0)):
                missing.append("hora entre las 10:00 y las 17:00")
    except (ValueError, KeyError, TypeError):
        missing.append("fecha y hora valida")

    # Direccion: solo obligatoria para envios.
    if command.get("type") == "envio":
        address = (command.get("address") or "").strip()
        # Esperamos al menos calle + numero + ciudad/CP. Heuristica: 3+ palabras y al menos un digito.
        if not address or len(address.split()) < 3 or not any(c.isdigit() for c in address):
            missing.append("direccion completa (calle, numero, codigo postal y ciudad)")

    return (len(missing) == 0, missing)


def _missing_data_message(command_type: str, missing: list[str]) -> str:
    """Construye un mensaje cordial pidiendo los datos que faltan."""
    if len(missing) == 1:
        falta = missing[0]
    else:
        falta = ", ".join(missing[:-1]) + f" y {missing[-1]}"

    if command_type == "envio":
        accion = "registrar la recogida"
    else:
        accion = "registrar tu cita"

    return (
        f"Antes de {accion} necesito que me confirmes: {falta}. "
        f"¿Me lo facilitas, por favor? 😊"
    )


async def process_ai_calendar_command(
    calendar_service: CalendarService,
    ai_response: str,
    attendee_phone: str = "",
    history: list[dict] | None = None,
) -> tuple[str, dict | None]:
    """
    1. Limpia el mensaje para el usuario
    2. Detecta si hay comando CONFIRMAR_*
    3. Valida que estan todos los datos requeridos en el historial
    4. Solo si la validacion pasa, crea el evento real en Google Calendar
    5. Devuelve:
       - user_message: texto limpio para enviar al cliente
       - created_event: evento creado o None
    """
    user_message = strip_confirmation_command(ai_response)
    command = extract_confirmation_command(ai_response)

    if not command:
        return user_message, None

    # Validacion en codigo: bloquea el LLM si trata de confirmar sin datos.
    is_valid, missing = _validate_appointment(command, history, attendee_phone)
    if not is_valid:
        logger.warning(
            "Bloqueado CONFIRMAR_%s por datos faltantes",
            command["type"].upper(),
            extra={"missing": missing, "command": command},
        )
        return _missing_data_message(command["type"], missing), None

    created_event = None

    if command["type"] == "cita":
        # Verificar disponibilidad del slot antes de crear el evento
        try:
            req_dt = datetime.fromisoformat(command["datetime_iso"])
            if req_dt.tzinfo is None:
                req_dt = req_dt.replace(tzinfo=MADRID_TZ)
            req_start = req_dt.astimezone(MADRID_TZ)
            req_end = req_start + timedelta(minutes=30)

            busy = calendar_service.get_busy_slots_for_day(command["datetime_iso"])
            if _overlaps(req_start, req_end, busy):
                req_time = req_start.strftime("%H:%M")
                req_date = req_start.strftime("%d/%m/%Y")
                available = calendar_service.get_available_slots(command["datetime_iso"])
                if available:
                    alts = ", ".join(available[:6])
                    return (
                        f"Lo siento 😊 La hora *{req_time}* del {req_date} ya está ocupada.\n"
                        f"Horas disponibles ese día: *{alts}*\n"
                        f"¿Cuál de estas horas te vendría mejor?"
                    ), None
                else:
                    return (
                        f"Lo siento 😊 El día {req_date} no tenemos horas disponibles entre las 10:00 y las 17:00.\n"
                        f"¿Te gustaría intentar con otro día?"
                    ), None
        except Exception as exc:
            logger.error(f"Error checking slot availability: {exc}", exc_info=True)

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