
import json
import os
import re
import smtplib
import hashlib
from pathlib import Path
from email.message import EmailMessage
from urllib.parse import urljoin, urldefrag

import requests
from bs4 import BeautifulSoup

HISTORIAL = Path("usados.json")
MAX_EVENTOS_POR_WEB = 500

PAGINAS = {
    "Atrápalo": [
        "https://www.atrapalo.com/entradas/madrid/teatro-y-danza/",
    ],
    "Taquilla.com": [
        "https://www.taquilla.com/espectaculos",
        "https://www.taquilla.com/espectaculos/teatro",
        "https://www.taquilla.com/espectaculos/humor-monologos/madrid/",
    ],
    "Entradas.com": [
        "https://www.entradas.com/city/madrid-370/this-month/",
        "https://www.entradas.com/city/madrid-370/teatro-84/",
    ],
}

PALABRAS_INTERES = re.compile(
    r"teatro|danza|ballet|mon[oó]logo|humor|comedia|"
    r"musical|espect[aá]culo|flamenco|impro|cabaret|"
    r"zarzuela|circo|actividad|magia|t[ií]teres",
    re.IGNORECASE,
)

PALABRAS_EXCLUIR = re.compile(
    r"pol[ií]tica de privacidad|aviso legal|"
    r"protecci[oó]n de datos|iniciar sesi[oó]n|"
    r"crear cuenta|contacto|cookies|"
    r"condiciones generales",
    re.IGNORECASE,
)

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    )
}


def cargar_historial():
    if not HISTORIAL.exists():
        return {}
    try:
        return json.loads(HISTORIAL.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(
            "No se puede leer usados.json. "
            "Se detiene el programa para no duplicar avisos."
        )


def guardar_historial(historial):
    HISTORIAL.write_text(
        json.dumps(historial, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalizar_url(url):
    url, _ = urldefrag(url)
    return url.rstrip("/")


def obtener_eventos(nombre_web, urls):
    eventos = {}

    for pagina in urls:
        print(f"[CONSULTA] {nombre_web}: {pagina}")

        try:
            respuesta = requests.get(
                pagina,
                headers=CABECERAS,
                timeout=30,
            )
            respuesta.raise_for_status()

            if "text/html" not in respuesta.headers.get(
                "Content-Type", ""
            ):
                print("[AVISO] La respuesta no es HTML.")
                continue

            soup = BeautifulSoup(respuesta.text, "html.parser")

            for enlace in soup.select("a[href]"):
                titulo = " ".join(enlace.get_text(" ", strip=True).split())
                url = normalizar_url(urljoin(pagina, enlace["href"]))

                if not titulo or len(titulo) < 4:
                    continue

                if not url.startswith(("https://", "http://")):
                    continue

                if PALABRAS_EXCLUIR.search(titulo):
                    continue

                dominio = url.split("/")[2].lower()
                if not (
                    dominio.endswith("atrapalo.com")
                    or dominio.endswith("taquilla.com")
                    or dominio.endswith("entradas.com")
                ):
                    continue

                if not PALABRAS_INTERES.search(titulo):
                    continue

                clave = hashlib.sha256(
                    f"{nombre_web}|{url}".encode("utf-8")
                ).hexdigest()

                eventos[clave] = {
                    "web": nombre_web,
                    "titulo": titulo,
                    "url": url,
                }

                if len(eventos) >= MAX_EVENTOS_POR_WEB:
                    break

        except requests.RequestException as error:
            print(f"[ERROR] No se pudo consultar {pagina}: {error}")

    return eventos


def enviar_correo(novedades):
    usuario = os.getenv("GMX_EMAIL", "").strip()
    contrasena = os.getenv("GMX_PASSWORD", "").strip()
    destinatario = os.getenv("DESTINATARIO_EMAIL", "").strip()

    if not usuario or not contrasena or not destinatario:
        print(
            "[AVISO] Falta configurar GMX_EMAIL, GMX_PASSWORD "
            "o DESTINATARIO_EMAIL. No se ha enviado correo."
        )
        return False

    mensaje = EmailMessage()
    mensaje["Subject"] = (
        f"Novedades de espectáculos en Madrid: {len(novedades)}"
    )
    mensaje["From"] = usuario
    mensaje["To"] = destinatario

    lineas = [
        "Se han detectado posibles novedades en las páginas vigiladas.",
        "",
    ]

    for evento in novedades:
        lineas.extend([
            f"Web: {evento['web']}",
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

    print(f"[CORREO] Enviado a {destinatario}")
    return True


def main():
    historial = cargar_historial()
    encontrados = {}
    errores = []

    for nombre_web, urls in PAGINAS.items():
        eventos = obtener_eventos(nombre_web, urls)

        if not eventos:
            errores.append(nombre_web)

        encontrados.update(eventos)

    print(f"[RESUMEN] Elementos detectados: {len(encontrados)}")

    if errores:
        print(
            "[AVISO] No se han detectado elementos en: "
            + ", ".join(errores)
            + ". Comprueba el registro de ejecución."
        )

    anteriores = set(historial.keys())
    nuevos_ids = set(encontrados.keys()) - anteriores

    if not historial:
        guardar_historial(encontrados)
        print(
            "[INICIO] Historial inicial guardado. "
            "No se envían avisos de eventos antiguos."
        )
        return

    novedades = [encontrados[i] for i in sorted(nuevos_ids)]

    if not novedades:
        print("[OK] No se han detectado novedades.")
        return

    print(f"[NOVEDADES] {len(novedades)} posibles elementos nuevos.")

    for evento in novedades:
        print(f"- {evento['web']}: {evento['titulo']}")

    # Enviar primero; actualizar el historial solo si se envía
    # el correo correctamente.
    if enviar_correo(novedades):
        historial.update(
            {clave: encontrados[clave] for clave in nuevos_ids}
        )
        guardar_historial(historial)
    else:
        raise RuntimeError(
            "No se ha enviado el correo. "
            "El historial no se ha actualizado."
        )


if __name__ == "__main__":
    main()
