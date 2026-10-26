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

HISTORIAL = Path("usados.json")
MAX_EVENTOS_POR_WEB = 800

# Solo páginas de categorías filtradas para Madrid. No se usa la portada
# genérica de Taquilla porque mezcla ciudades.
PAGINAS = {
    "Atrápalo": [
        "https://www.atrapalo.com/entradas/madrid/teatro-y-danza/",
    ],
    "Taquilla.com": [
        "https://www.taquilla.com/espectaculos/teatro/madrid",
        "https://www.taquilla.com/espectaculos/humor-monologos/madrid/",
        "https://www.taquilla.com/espectaculos/danza-ballet/madrid/",
        "https://www.taquilla.com/espectaculos/circo/madrid/",
    ],
    "Entradas.com": [
        "https://www.entradas.com/city/madrid-370/this-month/",
        "https://www.entradas.com/city/madrid-370/teatro-84/",
    ],
}

EXCLUIR_TITULO = re.compile(
    r"pol[ií]tica de privacidad|aviso legal|protecci[oó]n de datos|"
    r"iniciar sesi[oó]n|crear cuenta|contacto|cookies|condiciones|"
    r"ver todos|leer m[aá]s|reservar|comprar entradas|"
    r"aceptar todas|configurar cookies|mi cuenta|entradas\.com|taquilla\.com",
    re.IGNORECASE,
)
GENERICAS_TAQUILLA = {
    "/espectaculos", "/espectaculos/teatro",
    "/espectaculos/teatro/madrid",
    "/espectaculos/humor-monologos",
    "/espectaculos/humor-monologos/madrid",
    "/espectaculos/danza-ballet",
    "/espectaculos/danza-ballet/madrid",
    "/espectaculos/circo", "/espectaculos/circo/madrid",
}
CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def normalizar_texto(texto):
    return re.sub(r"\s+", " ", texto or "").strip()


def cargar_historial():
    if not HISTORIAL.exists():
        return {}
    try:
        data = json.loads(HISTORIAL.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("El historial debe ser un objeto JSON.")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"No se puede leer {HISTORIAL}: {exc}")


def guardar_historial(historial):
    temporal = HISTORIAL.with_suffix(".tmp")
    temporal.write_text(json.dumps(historial, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(HISTORIAL)


def url_limpia(url):
    url, _ = urldefrag(url)
    p = urlparse(url)
    # Quitar parámetros de seguimiento, pero conservar la ruta de la ficha.
    return p._replace(query="", fragment="").geturl().rstrip("/")


def es_url_generica(url, portal):
    p = urlparse(url)
    ruta = p.path.lower().rstrip("/") or "/"
    if portal == "Taquilla.com":
        if ruta in GENERICAS_TAQUILLA:
            return True
        # Rechazar enlaces de categoría de la forma /espectaculos/<categoria>
        # o /espectaculos/<categoria>/madrid.
        segmentos = [s for s in ruta.split("/") if s]
        if segmentos and segmentos[0] == "espectaculos":
            if len(segmentos) <= 2:
                return True
            if len(segmentos) == 3 and segmentos[-1] == "madrid":
                return True
    if portal == "Atrápalo":
        if ruta in {"/entradas", "/entradas/madrid/teatro-y-danza"}:
            return True
    if portal == "Entradas.com":
        if "/city/" in ruta or ruta in {"/", "/events"}:
            return True
    return False


def titulo_desde_tarjeta(heading, tarjeta):
    candidatos = [
        heading.get_text(" ", strip=True),
        heading.get("aria-label", ""),
        heading.get("title", ""),
    ]
    for selector in ["a[title]", "a[aria-label]", "img[alt]", "[class*=title]", "[class*=name]"]:
        for el in tarjeta.select(selector)[:4]:
            candidatos.extend([el.get("title", ""), el.get("aria-label", ""), el.get("alt", ""), el.get_text(" ", strip=True)])
    for candidato in candidatos:
        titulo = normalizar_texto(candidato)
        if 3 <= len(titulo) <= 180 and not EXCLUIR_TITULO.search(titulo):
            if titulo.lower() not in {"teatro", "danza y ballet", "humor/monólogos", "circo", "espectáculos"}:
                return titulo
    return ""


def ubicacion_madrid(texto, url, portal):
    texto = normalizar_texto(texto).lower()
    ruta = urlparse(url).path.lower()
    if any(x in texto for x in ["barcelona", "valencia", "sevilla", "bilbao", "málaga", "malaga", "alicante", "zaragoza"]):
        # Si también aparece Madrid, la ficha puede ser itinerante: aceptarla solo
        # cuando el texto identifica explícitamente una fecha en Madrid.
        if "madrid" not in texto:
            return False
    if "madrid" in texto or "comunidad de madrid" in texto:
        return True
    # En páginas específicas de Madrid, el listado de eventos está filtrado
    # geográficamente aunque el bloque de la tarjeta omita la ciudad.
    if portal == "Atrápalo" and "/entradas/madrid/" in ruta:
        return True
    if portal == "Taquilla.com" and ruta.endswith("/madrid"):
        return True
    return False


def extraer_eventos(html, pagina, portal):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()

    eventos = {}
    # Extraer desde títulos de tarjeta, no desde todos los enlaces de la web.
    for heading in soup.find_all(["h2", "h3", "h4"]):
        titulo = normalizar_texto(heading.get_text(" ", strip=True))
        if not titulo or len(titulo) < 3 or len(titulo) > 180:
            continue
        tarjeta = heading
        mejor_url = None
        contexto = ""
        for _ in range(5):
            if tarjeta is None or not getattr(tarjeta, "name", None):
                break
            contexto = normalizar_texto(tarjeta.get_text(" ", strip=True))
            enlaces = tarjeta.find_all("a", href=True)
            candidatos = []
            for a in enlaces:
                href = url_limpia(urljoin(pagina, a["href"]))
                if not href.startswith("https://"):
                    continue
                host = urlparse(href).netloc.lower().split(":")[0]
                if portal == "Atrápalo" and not (host == "atrapalo.com" or host.endswith(".atrapalo.com")):
                    continue
                if portal == "Taquilla.com" and not (host == "taquilla.com" or host.endswith(".taquilla.com")):
                    continue
                if portal == "Entradas.com" and not (host == "entradas.com" or host.endswith(".entradas.com")):
                    continue
                if es_url_generica(href, portal):
                    continue
                label = normalizar_texto(a.get_text(" ", strip=True)).lower()
                score = 0
                if href != url_limpia(pagina):
                    score += 2
                if any(w in label for w in ["entradas", "comprar", "más info", "mas info", "ver evento", "información"]):
                    score += 3
                if len(urlparse(href).path.strip("/").split("/")) >= 2:
                    score += 1
                candidatos.append((score, href))
            if candidatos:
                candidatos.sort(reverse=True)
                mejor_url = candidatos[0][1]
            if mejor_url and len(contexto) > len(titulo) + 5:
                break
            tarjeta = tarjeta.parent

        if not mejor_url or es_url_generica(mejor_url, portal):
            continue
        # El contexto de la tarjeta suele incluir sala, localidad, fechas y precio.
        if not ubicacion_madrid(contexto, pagina, portal):
            continue

        clave = hashlib.sha256(f"{portal}|{mejor_url.lower().rstrip('/')}".encode("utf-8")).hexdigest()
        eventos[clave] = {
            "web": portal,
            "titulo": titulo,
            "url": mejor_url,
            "contexto": contexto[:900],
        }
        if len(eventos) >= MAX_EVENTOS_POR_WEB:
            break

    return eventos


def consultar_web(portal, urls):
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)
    eventos = {}
    paginas_ok = 0
    for pagina in urls:
        print(f"[CONSULTA] {portal}: {pagina}")
        try:
            timeout = (8, 15) if portal == "Entradas.com" else (12, 30)
            respuesta = sesion.get(pagina, timeout=timeout)
            respuesta.raise_for_status()
            if "html" not in respuesta.headers.get("Content-Type", "").lower():
                print("[AVISO] La respuesta no es HTML.")
                continue
            respuesta.encoding = respuesta.apparent_encoding or respuesta.encoding
            encontrados = extraer_eventos(respuesta.text, respuesta.url, portal)
            paginas_ok += 1
            eventos.update(encontrados)
            print(f"[OK] Fichas de eventos de Madrid: {len(encontrados)}")
            time.sleep(1)
        except requests.RequestException as exc:
            print(f"[ERROR] No se pudo consultar {pagina}: {exc}")
    print(f"[RESUMEN WEB] {portal}: {paginas_ok}/{len(urls)} páginas; {len(eventos)} fichas válidas de Madrid.")
    return eventos, paginas_ok


def enviar_correo(evento):
    usuario = os.getenv("GMX_EMAIL", "").strip()
    contrasena = os.getenv("GMX_PASSWORD", "").strip()
    destinatario = os.getenv("DESTINATARIO_EMAIL", "").strip()
    if not usuario or not contrasena or not destinatario:
        raise RuntimeError("Faltan GMX_EMAIL, GMX_PASSWORD o DESTINATARIO_EMAIL en los secretos de GitHub.")

    mensaje = EmailMessage()
    mensaje["From"] = usuario
    mensaje["To"] = destinatario
    mensaje["Subject"] = f"Nueva actividad en Madrid: {evento['titulo']}"
    mensaje.set_content(
        "Se ha detectado una posible novedad en la cartelera de Madrid.\n\n"
        f"Espectáculo: {evento['titulo']}\n"
        f"Portal: {evento['web']}\n"
        f"Enlace directo: {evento['url']}\n\n"
        f"Información de la ficha: {evento.get('contexto', '')}\n\n"
        "Aviso automático. Comprueba las fechas y la disponibilidad en la web antes de comprar."
    )
    with smtplib.SMTP("mail.gmx.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(usuario, contrasena)
        smtp.send_message(mensaje)
    print(f"[CORREO] Enviado: {evento['titulo']} | {evento['url']}")


def main():
    historial = cargar_historial()
    actuales = {}
    webs_ok = set()

    for portal, urls in PAGINAS.items():
        eventos, paginas_ok = consultar_web(portal, urls)
        if paginas_ok:
            webs_ok.add(portal)
            actuales.update(eventos)

    if not webs_ok:
        raise RuntimeError("Ninguna web ha respondido correctamente.")

    # Evitar que el arreglo del filtro provoque un aluvión de correos:
    # la primera revisión con fichas reales establece una nueva referencia.
    historial_valido = {
        clave: valor for clave, valor in historial.items()
        if isinstance(valor, dict)
        and valor.get("web") in webs_ok
        and valor.get("url")
        and not es_url_generica(valor.get("url", ""), valor.get("web", ""))
        and ubicacion_madrid(valor.get("contexto", ""), valor.get("url", ""), valor.get("web", ""))
    }

    if not historial_valido:
        historial.update(actuales)
        guardar_historial(historial)
        print(f"[INICIO] Referencia de Madrid guardada: {len(actuales)} fichas. No se envían eventos antiguos.")
        return

    ids_anteriores = set(historial_valido)
    nuevas = [(clave, evento) for clave, evento in actuales.items() if clave not in ids_anteriores]

    if not nuevas:
        print("[SIN NOVEDADES] No hay nuevas fichas de eventos de Madrid.")
    else:
        print(f"[NOVEDADES] {len(nuevas)} nuevas fichas concretas de Madrid.")
        enviados = []
        for clave, evento in nuevas:
            try:
                enviar_correo(evento)
                enviados.append((clave, evento))
            except Exception as exc:
                print(f"[ERROR CORREO] {evento['titulo']}: {exc}")
                raise
        for clave, evento in enviados:
            historial[clave] = evento
        guardar_historial(historial)
        print(f"[OK] Historial actualizado con {len(enviados)} novedades enviadas.")

    # Guardar las fichas actuales de Madrid, sin registrar enlaces genéricos.
    historial.update(actuales)
    guardar_historial(historial)


if __name__ == "__main__":
    main()
