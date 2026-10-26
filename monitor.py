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
CONFIG_VERSION = 2

PAGINAS = {
    "Atrápalo": [
        "https://www.atrapalo.com/entradas/madrid/madrid/teatro-y-danza/",
    ],
    "Taquilla.com": [
        "https://www.taquilla.com/espectaculos/teatro/madrid",
        "https://www.taquilla.com/espectaculos/danza/madrid",
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
        # Rechazar categorías conocidas, pero permitir fichas concretas
        # que también usan la forma /espectaculos/nombre-del-evento.
        segmentos = [s for s in ruta.split("/") if s]
        categorias = {
            "teatro", "humor-monologos", "danza-ballet", "circo",
            "musicales", "magia", "espectaculos-para-ninos", "flamenco",
            "opera-clasica", "otros-espectaculos", "actividades",
        }
        if segmentos and segmentos[0] == "espectaculos":
            if len(segmentos) == 2 and segmentos[1] in categorias:
                return True
            if len(segmentos) == 3 and segmentos[1] in categorias and segmentos[-1] == "madrid":
                return True
    if portal == "Atrápalo":
        if ruta == "/entradas" or "teatro-y-danza" in ruta or ruta.endswith("/madrid/madrid"):
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

    def guardar_evento(titulo, href, contexto):
        titulo = normalizar_texto(titulo)
        href = url_limpia(urljoin(pagina, href))
        if not titulo or len(titulo) < 3 or len(titulo) > 180:
            return
        if EXCLUIR_TITULO.search(titulo) or es_url_generica(href, portal):
            return
        host = urlparse(href).netloc.lower().split(":")[0]
        if portal == "Atrápalo" and not (host == "atrapalo.com" or host.endswith(".atrapalo.com")):
            return
        if portal == "Taquilla.com" and not (host == "taquilla.com" or host.endswith(".taquilla.com")):
            return
        if portal == "Entradas.com" and not (host == "entradas.com" or host.endswith(".entradas.com")):
            return
        if not ubicacion_madrid(contexto, pagina, portal):
            return
        clave = hashlib.sha256(f"{portal}|{href.lower().rstrip('/')} ".strip().encode("utf-8")).hexdigest()
        eventos[clave] = {
            "web": portal,
            "titulo": titulo,
            "url": href,
            "contexto": contexto[:900],
        }

    # Primera pasada: fichas enlazadas desde encabezados de las tarjetas.
    for heading in soup.find_all(["h2", "h3", "h4"]):
        tarjeta = heading
        mejor_url = None
        contexto = ""
        titulo = normalizar_texto(heading.get_text(" ", strip=True))
        for _ in range(7):
            if tarjeta is None or not getattr(tarjeta, "name", None):
                break
            contexto = normalizar_texto(tarjeta.get_text(" ", strip=True))
            enlaces = tarjeta.find_all("a", href=True)
            candidatos = []
            for a in enlaces:
                href = url_limpia(urljoin(pagina, a["href"]))
                if es_url_generica(href, portal):
                    continue
                label = normalizar_texto(a.get_text(" ", strip=True)).lower()
                score = 0
                if href != url_limpia(pagina):
                    score += 2
                if any(w in label for w in ["entradas", "comprar", "más info", "mas info", "ver evento", "reservar", "ver oferta"]):
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
        if mejor_url:
            guardar_evento(titulo, mejor_url, contexto)

    # Segunda pasada de respaldo para Atrápalo, donde el título y el botón
    # de reserva pueden estar en bloques distintos del HTML.
    if portal == "Atrápalo" and not eventos:
        for a in soup.find_all("a", href=True):
            href = url_limpia(urljoin(pagina, a["href"]))
            p = urlparse(href)
            host = p.netloc.lower().split(":")[0]
            if not (host == "atrapalo.com" or host.endswith(".atrapalo.com")):
                continue
            if "/entradas/" not in p.path.lower() or es_url_generica(href, portal):
                continue
            tarjeta = a
            contexto = ""
            titulo = ""
            for _ in range(6):
                if tarjeta is None or not getattr(tarjeta, "name", None):
                    break
                contexto = normalizar_texto(tarjeta.get_text(" ", strip=True))
                h = tarjeta.find(["h2", "h3", "h4"])
                if h:
                    titulo = normalizar_texto(h.get_text(" ", strip=True))
                if not titulo:
                    for selector in ["[class*=title]", "[class*=name]", "a[title]", "img[alt]"]:
                        item = tarjeta.select_one(selector)
                        if item:
                            titulo = normalizar_texto(item.get("title") or item.get("alt") or item.get_text(" ", strip=True))
                            if titulo:
                                break
                if titulo and len(contexto) > len(titulo) + 4:
                    break
                tarjeta = tarjeta.parent
            if not titulo:
                titulo = normalizar_texto(a.get("title") or a.get_text(" ", strip=True))
            if titulo.lower() in {"reservar", "comprar", "entradas", "ver entradas", "más información", "mas informacion"}:
                continue
            guardar_evento(titulo, href, contexto or titulo)

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


def enviar_resumen_correo(eventos):
    usuario = os.getenv("GMX_EMAIL", "").strip()
    contrasena = os.getenv("GMX_PASSWORD", "").strip()
    destinatario = os.getenv("DESTINATARIO_EMAIL", "").strip()
    if not usuario or not contrasena or not destinatario:
        raise RuntimeError("Faltan GMX_EMAIL, GMX_PASSWORD o DESTINATARIO_EMAIL en los secretos de GitHub.")

    def escapar(valor):
        import html
        return html.escape(str(valor or ""))

    tarjetas_html = []
    lineas_texto = [
        f"Se han detectado {len(eventos)} novedades de teatro y danza/ballet en Madrid.",
        "",
    ]
    for evento in eventos:
        titulo = evento.get("titulo", "Espectáculo")
        url = evento.get("url", "")
        portal = evento.get("web", "")
        contexto = evento.get("contexto", "")
        tarjetas_html.append(
            '<div style="border:1px solid #d8dee8;border-radius:10px;padding:16px;margin:0 0 14px;background:#ffffff;">'
            f'<div style="font-size:12px;color:#536273;margin-bottom:7px;">{escapar(portal)} · MADRID</div>'
            f'<div style="font-size:18px;font-weight:700;line-height:1.35;margin-bottom:8px;">{escapar(titulo)}</div>'
            f'<div style="font-size:13px;line-height:1.5;color:#4b5563;margin-bottom:12px;">{escapar(contexto)}</div>'
            f'<a href="{escapar(url)}" style="display:inline-block;background:#155eef;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:6px;font-weight:600;">Ver obra y entradas</a>'
            '</div>'
        )
        lineas_texto.extend([f"{titulo} — {portal}", url, contexto, ""])

    html_body = (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#f3f5f8;font-family:Arial,Helvetica,sans-serif;color:#172033;">'
        '<div style="max-width:720px;margin:0 auto;">'
        '<div style="background:#172033;color:#ffffff;padding:22px;border-radius:12px 12px 0 0;">'
        '<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#cbd5e1;">Avisos de espectáculos</div>'
        f'<h1 style="font-size:24px;margin:8px 0 4px;">{len(eventos)} novedades en Madrid</h1>'
        '<div style="font-size:14px;color:#e2e8f0;">Teatro · Danza y ballet</div>'
        '</div><div style="padding:18px;background:#f3f5f8;">'
        + "".join(tarjetas_html)
        + '<p style="font-size:12px;color:#64748b;">Aviso automático. Comprueba las fechas y la disponibilidad en la ficha antes de comprar.</p>'
        + '</div></div></body></html>'
    )

    mensaje = EmailMessage()
    mensaje["From"] = usuario
    mensaje["To"] = destinatario
    mensaje["Subject"] = f"Cartelera Madrid: {len(eventos)} novedad(es) de teatro y danza"
    mensaje.set_content("\n".join(lineas_texto))
    mensaje.add_alternative(html_body, subtype="html")

    with smtplib.SMTP("mail.gmx.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(usuario, contrasena)
        smtp.send_message(mensaje)
    print(f"[CORREO] Resumen visual enviado correctamente: {len(eventos)} fichas en un solo correo.")

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

    # Al cambiar las categorías vigiladas, establecer una referencia nueva una
    # sola vez evita mandar de golpe decenas de fichas antiguas o de categorías
    # que ya no se quieren seguir.
    if historial.get("_config_version") != CONFIG_VERSION:
        historial.update(actuales)
        historial["_config_version"] = CONFIG_VERSION
        guardar_historial(historial)
        print(f"[INICIO] Referencia inicial guardada: {len(actuales)} fichas actuales. No se envían avisos antiguos.")
        return

    ids_anteriores = {
        clave for clave, valor in historial.items()
        if isinstance(valor, dict)
        and valor.get("web") in webs_ok
        and valor.get("url")
        and not es_url_generica(valor.get("url", ""), valor.get("web", ""))
    }
    nuevas = [(clave, evento) for clave, evento in actuales.items() if clave not in ids_anteriores]

    if not nuevas:
        print("[SIN NOVEDADES] No hay nuevas fichas de teatro y danza/ballet en Madrid.")
    else:
        eventos_nuevos = [evento for _, evento in nuevas]
        print(f"[NOVEDADES] {len(eventos_nuevos)} nuevas fichas concretas de teatro y danza/ballet.")
        enviar_resumen_correo(eventos_nuevos)
        for clave, evento in nuevas:
            historial[clave] = evento
        guardar_historial(historial)
        print(f"[OK] Historial actualizado con {len(nuevas)} novedades enviadas.")

    historial.update(actuales)
    historial["_config_version"] = CONFIG_VERSION
    guardar_historial(historial)


if __name__ == "__main__":
    main()
