import hashlib
import json
import os
import re
import smtplib
import time
from pathlib import Path
from email.message import EmailMessage
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HISTORIAL = Path("usados.json")
MAX_EVENTOS_POR_WEB = 700

PAGINAS = {
    "Atrápalo": [
        "https://www.atrapalo.com/entradas/madrid/teatro-y-danza/",
        "https://www.atrapalo.com/entradas/?outstanding=yes",
    ],
    "Taquilla.com": [
        "https://www.taquilla.com/espectaculos/teatro/madrid",
        "https://www.taquilla.com/espectaculos/humor-monologos?t10city=Madrid&t10tag=13",
        "https://www.taquilla.com/espectaculos/humor-monologos",
        "https://www.taquilla.com/espectaculos",
    ],
    "Entradas.com": [
        "https://www.entradas.com/city/madrid-370/this-month/",
        "https://www.entradas.com/city/madrid-370/teatro-84/",
    ],
}

PALABRAS_EXCLUIR = re.compile(
    r"pol[ií]tica de privacidad|aviso legal|protecci[oó]n de datos|"
    r"iniciar sesi[oó]n|crear cuenta|contacto|cookies|"
    r"condiciones generales|ver todos|leer m[aá]s|reservar|"
    r"comprar entradas|entradas.com|taquilla.com|"
    r"aceptar todas|configurar cookies|mi cuenta",
    re.IGNORECASE,
)

RUTAS_GENERALES_ATRAPALO = {
    "/entradas",
    "/entradas/madrid",
    "/entradas/madrid/teatro-y-danza",
    "/entradas/teatro-y-danza",
    "/entradas/?outstanding=yes",
}

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def crear_sesion():
    sesion = requests.Session()
    reintentos = Retry(
        total=1,
        connect=1,
        read=0,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    sesion.mount("https://", HTTPAdapter(max_retries=reintentos))
    sesion.headers.update(CABECERAS)
    return sesion


def cargar_historial():
    if not HISTORIAL.exists():
        return {}
    try:
        datos = json.loads(HISTORIAL.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("El historial no contiene un objeto JSON.")
        return datos
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(
            f"No se puede leer {HISTORIAL}: {error}. "
            "No se modificará el historial."
        )


def guardar_historial(historial):
    temporal = HISTORIAL.with_suffix(".tmp")
    temporal.write_text(
        json.dumps(historial, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporal.replace(HISTORIAL)


def normalizar_url(url):
    url, _ = urldefrag(url)
    partes = urlparse(url)
    # Se quita la consulta para que el mismo evento no se duplique
    # cuando aparece con parámetros de seguimiento.
    return partes._replace(query="", fragment="").geturl().rstrip("/")


def titulo_del_enlace(enlace):
    candidatos = [
        enlace.get_text(" ", strip=True),
        enlace.get("aria-label", ""),
        enlace.get("title", ""),
    ]
    imagen = enlace.find("img")
    if imagen:
        candidatos.extend([imagen.get("alt", ""), imagen.get("title", "")])

    # Algunos portales colocan el nombre en un encabezado o tarjeta
    # cercana en lugar de hacerlo directamente en el enlace.
    for selector in ["h2", "h3", "h4"]:
        encabezado = enlace.find(selector)
        if encabezado:
            candidatos.append(encabezado.get_text(" ", strip=True))

    for candidato in candidatos:
        texto = " ".join(str(candidato).split())
        if 4 <= len(texto) <= 180 and not PALABRAS_EXCLUIR.search(texto):
            return texto
    return ""


def es_enlace_de_evento(url, titulo, nombre_web):
    partes = urlparse(url)
    host = partes.netloc.lower().split(":")[0]
    ruta = partes.path.lower().rstrip("/")

    if not titulo or len(titulo) < 4 or len(titulo) > 180:
        return False

    if PALABRAS_EXCLUIR.search(titulo):
        return False

    if nombre_web == "Atrápalo":
        if not (host == "atrapalo.com" or host.endswith(".atrapalo.com")):
            return False
        if not ruta.startswith("/entradas/"):
            return False
        if ruta in RUTAS_GENERALES_ATRAPALO:
            return False
        # Descarta filtros y páginas de navegación, pero no exige un
        # formato concreto de identificador: Atrápalo puede cambiarlo.
        if any(x in ruta for x in ["/categoria/", "/provincia/", "/ciudad/"]):
            return False
        return len([p for p in ruta.split("/") if p]) >= 2

    if nombre_web == "Taquilla.com":
        if not (host == "taquilla.com" or host.endswith(".taquilla.com")):
            return False
        if ruta in ("", "/espectaculos", "/espectaculos/teatro"):
            return False
        return ruta.startswith("/espectaculos/")

    if nombre_web == "Entradas.com":
        if not (host == "entradas.com" or host.endswith(".entradas.com")):
            return False
        if "/city/" in ruta or ruta in ("", "/events"):
            return False
        return len(ruta.strip("/")) > 5

    return False


def obtener_eventos(nombre_web, urls):
    eventos = {}
    paginas_correctas = 0
    sesion = crear_sesion()

    for pagina in urls:
        print(f"[CONSULTA] {nombre_web}: {pagina}")
        try:
            # Entradas.com ya agotó incluso 60 segundos; limitar la
            # espera evita que bloquee toda la revisión de los demás.
            timeout = (10, 20) if nombre_web == "Entradas.com" else (15, 35)
            respuesta = sesion.get(pagina, timeout=timeout)
            respuesta.raise_for_status()

            tipo = respuesta.headers.get("Content-Type", "").lower()
            if "html" not in tipo:
                print("[AVISO] La respuesta no es HTML; se omite esta página.")
                continue

            soup = BeautifulSoup(respuesta.text, "html.parser")
            texto_pagina = soup.get_text(" ", strip=True)
            if len(texto_pagina) < 200:
                print("[AVISO] Página demasiado vacía; se omite esta página.")
                continue

            paginas_correctas += 1
            encontrados_pagina = 0

            for enlace in soup.select("a[href]"):
                titulo = titulo_del_enlace(enlace)
                url = normalizar_url(urljoin(pagina, enlace["href"]))

                if not es_enlace_de_evento(url, titulo, nombre_web):
                    continue

                clave = hashlib.sha256(
                    f"{nombre_web}|{url}".encode("utf-8")
                ).hexdigest()

                eventos[clave] = {
                    "web": nombre_web,
                    "titulo": titulo,
                    "url": url,
                }
                encontrados_pagina += 1

                if len(eventos) >= MAX_EVENTOS_POR_WEB:
                    break

            print(
                f"[OK] Página consultada. "
                f"Enlaces candidatos: {encontrados_pagina}"
            )
            time.sleep(1)

        except requests.RequestException as error:
            print(f"[ERROR] No se pudo consultar {pagina}: {error}")

    print(
        f"[RESUMEN WEB] {nombre_web}: "
        f"{paginas_correctas}/{len(urls)} páginas consultadas; "
        f"{len(eventos)} enlaces candidatos."
    )
    return eventos, paginas_correctas


def enviar_correo(novedades):
    usuario = os.getenv("GMX_EMAIL", "").strip()
    contrasena = os.getenv("GMX_PASSWORD", "").strip()
    destinatario = os.getenv("DESTINATARIO_EMAIL", "").strip()

    if not usuario or not contrasena or not destinatario:
        raise RuntimeError(
            "Falta configurar GMX_EMAIL, GMX_PASSWORD "
            "o DESTINATARIO_EMAIL en GitHub Secrets."
        )

    mensaje = EmailMessage()
    mensaje["Subject"] = f"Novedades de espectáculos en Madrid: {len(novedades)}"
    mensaje["From"] = usuario
    mensaje["To"] = destinatario

    lineas = [
        "Posibles novedades detectadas en las páginas vigiladas.",
        "Comprueba los detalles en la web antes de comprar.",
        "",
    ]
    for evento in novedades:
        lineas.extend([
            f"Portal: {evento['web']}",
            f"Espectáculo: {evento['titulo']}",
            f"Enlace: {evento['url']}",
            "",
        ])
    mensaje.set_content("\n".join(lineas))

    with smtplib.SMTP("mail.gmx.com", 587, timeout=30) as servidor:
        servidor.ehlo()
        servidor.starttls()
        servidor.ehlo()
        servidor.login(usuario, contrasena)
        servidor.send_message(mensaje)

    print(f"[CORREO] Enviado correctamente a {destinatario}")


def main():
    historial = cargar_historial()
    encontrados = {}
    webs_correctas = set()

    for nombre_web, urls in PAGINAS.items():
        eventos, paginas_correctas = obtener_eventos(nombre_web, urls)
        if paginas_correctas > 0:
            webs_correctas.add(nombre_web)
            encontrados.update(eventos)
        else:
            print(
                f"[AVISO] {nombre_web} no se ha podido consultar en esta "
                "ejecución. Se conserva su historial y se continúa con las demás."
            )

    if not webs_correctas:
        raise RuntimeError(
            "Ninguna web se ha podido consultar. No se modifica el historial."
        )

    print(f"[RESUMEN] Eventos candidatos en webs consultadas: {len(encontrados)}")

    # Si una web no responde, se conserva intacto su historial.
    historial_por_web = {
        clave: valor
        for clave, valor in historial.items()
        if not isinstance(valor, dict) or valor.get("web") not in webs_correctas
    }

    webs_ya_inicializadas = {
        valor.get("web")
        for valor in historial.values()
        if isinstance(valor, dict) and valor.get("web")
    }

    # La primera vez que una fuente produce resultados, se toma como
    # referencia inicial y no se envía un correo masivo por eventos antiguos.
    novedades = []
    para_guardar = {}

    for clave, evento in encontrados.items():
        if clave not in historial:
            if evento["web"] in webs_ya_inicializadas:
                novedades.append(evento)
            para_guardar[clave] = evento

    if novedades:
        print(f"[NOVEDADES] {len(novedades)} posibles eventos nuevos.")
        enviar_correo(novedades)
    else:
        print("[OK] No hay novedades nuevas que requieran correo.")

    historial_actualizado = dict(historial_por_web)
    historial_actualizado.update(historial)
    historial_actualizado.update(para_guardar)
    guardar_historial(historial_actualizado)
    print("[OK] Historial actualizado sin borrar los datos de webs inaccesibles.")


if __name__ == "__main__":
    main()
