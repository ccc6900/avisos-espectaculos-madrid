
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

PALABRAS_INTERES = re.compile(
    r"teatro|danza|ballet|mon[oó]logo|humor|comedia|musical|"
    r"espect[aá]culo|flamenco|impro|cabaret|zarzuela|circo|"
    r"magia|t[ií]teres|show|stand.?up",
    re.IGNORECASE,
)

PALABRAS_EXCLUIR = re.compile(
    r"pol[ií]tica de privacidad|aviso legal|protecci[oó]n de datos|"
    r"iniciar sesi[oó]n|crear cuenta|contacto|cookies|"
    r"condiciones generales|ver todos|leer m[aá]s|reservar|"
    r"comprar entradas|entradas.com|taquilla.com",
    re.IGNORECASE,
)

RUTAS_NO_EVENTO = re.compile(
    r"/(login|registro|contacto|ayuda|privacidad|cookies|"
    r"condiciones|entradas/madrid/teatro-y-danza|"
    r"city/madrid-370/teatro-84)(/|$)",
    re.IGNORECASE,
)

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}


def crear_sesion():
    sesion = requests.Session()
    reintentos = Retry(
        total=2,
        connect=2,
        read=1,
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
    return partes._replace(fragment="", query="").geturl().rstrip("/")


def es_enlace_de_evento(url, titulo, nombre_web):
    partes = urlparse(url)
    ruta = partes.path.lower()
    titulo = titulo.strip()

    if not titulo or len(titulo) < 4 or len(titulo) > 180:
        return False

    if PALABRAS_EXCLUIR.search(titulo):
        return False

    if RUTAS_NO_EVENTO.search(ruta):
        return False

    # Atrápalo suele identificar las fichas con un identificador _e...
    if nombre_web == "Atrápalo":
        return (
            partes.netloc.lower().endswith("atrapalo.com")
            and "/entradas/" in ruta
            and bool(re.search(r"_e\d+", ruta))
        )

    # En Taquilla.com se descartan las páginas de categorías.
    if nombre_web == "Taquilla.com":
        if not partes.netloc.lower().endswith("taquilla.com"):
            return False
        if ruta in ("/", "/espectaculos", "/espectaculos/teatro"):
            return False
        if ruta.startswith("/espectaculos/"):
            return True
        return False

    # En Entradas.com solo se aceptan enlaces del propio portal
    # que parezcan fichas y no categorías generales.
    if nombre_web == "Entradas.com":
        if not partes.netloc.lower().endswith("entradas.com"):
            return False
        if "/city/" in ruta or ruta in ("/", "/events"):
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
            respuesta = sesion.get(pagina, timeout=(15, 60))
            respuesta.raise_for_status()

            tipo = respuesta.headers.get("Content-Type", "").lower()
            if "html" not in tipo:
                print("[AVISO] La respuesta no es HTML.")
                continue

            soup = BeautifulSoup(respuesta.text, "html.parser")

            # Si la web devuelve una página de bloqueo o sin contenido,
            # no la consideramos una consulta válida.
            texto_pagina = soup.get_text(" ", strip=True).lower()
            if len(texto_pagina) < 200:
                print("[AVISO] Página demasiado vacía para procesarla.")
                continue

            encontrados_pagina = 0

            for enlace in soup.select("a[href]"):
                titulo = " ".join(enlace.get_text(" ", strip=True).split())
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

            paginas_correctas += 1
            print(
                f"[OK] Página consultada. "
                f"Enlaces de eventos candidatos: {encontrados_pagina}"
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
    mensaje["Subject"] = (
        f"Novedades de espectáculos en Madrid: {len(novedades)}"
    )
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
    webs_correctas = {}
    errores = []

    for nombre_web, urls in PAGINAS.items():
        eventos, paginas_correctas = obtener_eventos(nombre_web, urls)
        encontrados.update(eventos)
        webs_correctas[nombre_web] = paginas_correctas

        if paginas_correctas == 0:
            errores.append(nombre_web)

    print(f"[RESUMEN] Eventos candidatos detectados: {len(encontrados)}")

    # Si falla una web entera, no actualizamos el historial ni enviamos
    # alertas parciales: así se evita marcar novedades sin haber
    # comprobado todos los portales.
    if errores:
        raise RuntimeError(
            "No se pudieron consultar páginas de: "
            + ", ".join(errores)
            + ". El historial no se ha actualizado."
        )

    if not historial:
        guardar_historial(encontrados)
        print(
            "[INICIO] Historial inicial guardado. "
            "No se envían avisos de eventos antiguos."
        )
        return

    anteriores = set(historial.keys())
    nuevos_ids = set(encontrados.keys()) - anteriores
    novedades = [encontrados[i] for i in sorted(nuevos_ids)]

    if not novedades:
        print("[OK] No se han detectado novedades.")
        return

    print(f"[NOVEDADES] {len(novedades)} enlaces nuevos candidatos.")

    for evento in novedades:
        print(f"- {evento['web']}: {evento['titulo']}")

    enviar_correo(novedades)

    # Solo actualizamos el historial después de enviar correctamente.
    historial.update(
        {clave: encontrados[clave] for clave in nuevos_ids}
    )
    guardar_historial(historial)
    print("[OK] Historial actualizado.")


if __name__ == "__main__":
    main()
