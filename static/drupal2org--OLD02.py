#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drupal2org.py — Migración de nodos Drupal 7 a ficheros .org para org-static-blog

Uso:
  python3 drupal2org.py --nid 452          # Un nodo concreto (modo depuración)
  python3 drupal2org.py --all              # Todos los nodos procesables

Salida:
  ~/migracion-drupal/org-output/YYYY-MM-DD-slug.org
  ~/migracion-drupal/rsync-scripts/rsync-NID.sh
"""

import sys
import os
import re
import argparse
import subprocess
import unicodedata
from datetime import datetime
from urllib.parse import quote, unquote

import pymysql

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────

DB_CONFIG = {
    "host":     "localhost",
    "user":     "godestdb",
    "password": "nwoFPIHSp8VNKBLf0aI1",
    "db":       "godestdbv7_drupal",
    "charset":  "utf8mb4",
}

# Patrones de URL local de Drupal — de más específico a menos específico
DRUPAL_FILES_URL_PATTERNS = [
    "http://godest.vivencias.net/sites/godest.vivencias.net/files",
    "http://godest.vivencias.net/sites/godestv7.vivencias.net/files",
    "http://godestv7.vivencias.net/sites/godest.vivencias.net/files",
    "http://godestv7.vivencias.net/sites/godestv7.vivencias.net/files",
    "https://godest.vivencias.net/sites/godest.vivencias.net/files",
    "https://godest.vivencias.net/sites/godestv7.vivencias.net/files",
    "https://godestv7.vivencias.net/sites/godest.vivencias.net/files",
    "https://godestv7.vivencias.net/sites/godestv7.vivencias.net/files",
    "/sites/godest.vivencias.net/files",
    "/sites/godestv7.vivencias.net/files",
    # URL absoluta sin /sites/ (patrón antiguo pre-v7)
    "http://godest.vivencias.net/files",
    "https://godest.vivencias.net/files",
    # Rutas relativas antiguas
    "/files",
]

# Smileys de Drupal core (origen distinto a drupal-files)
DRUPAL_MISC_URL_PATTERNS = ["/misc/smileys", "misc/smileys"]
DRUPAL_MISC_LOCAL  = "/home/godo/drupal-7.103/misc/smileys"
DRUPAL_MISC_STATIC = "/static/img/smileys"

# Rutas locales
DRUPAL_FILES_LOCAL = "/home/godo/drupal-files"

# Destinos estáticos — nueva estructura organizada
STATIC_IMG   = "/static/img"    # imágenes
STATIC_FILES = "/static/files"  # adjuntos descargables

# URL base del blog (para enlaces absolutos de adjuntos)
BLOG_BASE_URL = "https://godest.vivencias.net"

# URI pública de Drupal
DRUPAL_PUBLIC_URI = "public://"

# Extensiones consideradas imágenes
IMG_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}

# Directorio de salida de los .org
OUTPUT_DIR = os.path.expanduser("~/migracion-drupal/org-output")

# Directorio de salida de los scripts rsync
RSYNC_DIR = os.path.expanduser("~/migracion-drupal/rsync-scripts")

# URL base del formulario de comentarios
COMMENT_FORM_BASE = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSeGnXB3zyaNVYcb_2EoO8C5HRc6Emx-2rv2QS6OWNFz1yYSBQ/"
    "viewform?usp=pp_url&entry.55081144="
)

# Tipos de nodo a procesar
TIPOS_A_PROCESAR = [
    "story", "blog", "page",
    "concierto", "especial", "event",
    "musica", "imagen", "image",
    "flexinode-1", "flexinode-2",
    "quotes", "forum",
]

# ─── UTILIDADES ───────────────────────────────────────────────────────────────

def slugify(texto):
    """Convierte un título en slug para nombre de fichero."""
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto)
    return texto.strip("-")


def strip_html(html):
    """Elimina etiquetas HTML y devuelve texto plano."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def primeras_palabras(texto, max_chars=160):
    """Recorta a ~160 caracteres sin cortar palabras."""
    if len(texto) <= max_chars:
        return texto
    recortado = texto[:max_chars]
    ultimo_espacio = recortado.rfind(" ")
    if ultimo_espacio > 0:
        recortado = recortado[:ultimo_espacio]
    return recortado + "…"


def es_imagen(nombre):
    """Devuelve True si la extensión del fichero es de imagen."""
    ext = os.path.splitext(nombre)[1].lower()
    return ext in IMG_EXTENSIONS


def uri_publica_a_local(uri):
    """Convierte URI pública Drupal (public://...) a ruta local."""
    if uri.startswith(DRUPAL_PUBLIC_URI):
        relativa = uri[len(DRUPAL_PUBLIC_URI):]
        return os.path.join(DRUPAL_FILES_LOCAL, relativa)
    return None


def url_a_ruta_local(url):
    """
    Dada una URL de imagen de Drupal, devuelve
    (nombre_fichero, origen_local, destino_static).
    Retorna None si no es un fichero local de Drupal.
    """
    url_limpia = unquote(url.split("?")[0])
    # Limpiar doble ruta bug
    url_limpia = re.sub(r'(/files(?:/images)?)/+files(?:/images)?/', r'\1/', url_limpia)

    # Smileys de Drupal core
    for patron in DRUPAL_MISC_URL_PATTERNS:
        if url_limpia.startswith(patron):
            nombre = url_limpia[len(patron):]
            return nombre, DRUPAL_MISC_LOCAL + nombre, DRUPAL_MISC_STATIC + nombre

    # Ficheros en drupal-files
    for patron in DRUPAL_FILES_URL_PATTERNS:
        if url_limpia.startswith(patron):
            ruta_relativa = url_limpia[len(patron):]
            nombre_fichero = os.path.basename(ruta_relativa)
            origen_local = DRUPAL_FILES_LOCAL + ruta_relativa
            destino_url = (STATIC_IMG if es_imagen(nombre_fichero) else STATIC_FILES) + "/" + nombre_fichero
            return ruta_relativa, origen_local, destino_url

    return None


# ─── PROCESAMIENTO HTML ───────────────────────────────────────────────────────

def extraer_bloques_codigo(html):
    """
    FIX #1: Extrae bloques de código antes de pandoc para preservar
    su contenido exacto (especialmente <f8> y otros tags dentro del código).
    Captura tanto <pre><code> como <code type="..."> multilinea.
    Los reemplaza por placeholders y los restaura después de pandoc.
    """
    placeholders_codigo = {}
    contador = [0]

    def reemplazar_codigo(match):
        contenido = match.group(1)
        # Escapar < y > para que pandoc no los interprete como HTML
        contenido_escapado = (contenido
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
        contador[0] += 1
        key = f"__CODE_PLACEHOLDER_{contador[0]}__"
        placeholders_codigo[key] = contenido_escapado
        return f"<pre><code>{key}</code></pre>"

    # Capturar <pre><code ...>...</code></pre>
    html_mod = re.sub(
        r'<pre[^>]*><code[^>]*>(.*?)</code></pre>',
        reemplazar_codigo,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Capturar también <code type="...">contenido multilinea</code>
    # (antes de que normalizar_html los convierta a <pre><code>)
    def reemplazar_code_type(match):
        contenido = match.group(1)
        if "\n" not in contenido:
            return match.group(0)  # inline, no tocar
        contenido_escapado = (contenido
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
        contador[0] += 1
        key = f"__CODE_PLACEHOLDER_{contador[0]}__"
        placeholders_codigo[key] = contenido_escapado
        return f"<pre><code>{key}</code></pre>"

    html_mod = re.sub(
        r'<code[^>]*>(.*?)</code>',
        reemplazar_code_type,
        html_mod,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return html_mod, placeholders_codigo


def restaurar_bloques_codigo(org_text, placeholders_codigo):
    """
    Restaura los bloques de código en el texto Org, reemplazando
    los placeholders por bloques #+begin_example con el contenido original.
    """
    for key, contenido in placeholders_codigo.items():
        # Desescapar entidades básicas
        contenido = contenido.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        bloque = f"\n#+begin_example\n{contenido}\n#+end_example\n"
        # Usar re.sub con lambda para evitar que \ en rutas Windows se interpreten
        # como secuencias de escape en el string de reemplazo
        patron = r'#+begin[_\s]example\s*\n.*?' + re.escape(key) + r'.*?\n#+end[_\s]example'
        if re.search(patron, org_text, flags=re.DOTALL | re.IGNORECASE):
            org_text = re.sub(
                patron,
                lambda m, b=bloque.strip(): b,
                org_text,
                flags=re.DOTALL | re.IGNORECASE,
            )
        else:
            # Placeholder suelto
            org_text = org_text.replace(key, bloque)
    return org_text


def normalizar_saltos_linea(html):
    """
    Convierte \\n simples entre texto en separadores de párrafo.
    También detecta párrafos implícitos: texto seguido de \n y más texto,
    sin etiquetas de bloque entre medias.
    """
    html = html.replace('\r\n', '\n').replace('\r', '\n')
    # Insertar </p><p> entre bloques de texto separados por \n simple
    # cuando no hay etiquetas de bloque de por medio
    html = re.sub(
        r'(?<![>\n])\n(?![\n<])',
        '<br>\n', html
    )
    # Convertir secuencias de <br> seguidos de texto que empieza con
    # negrita/strong al principio → separar en párrafos
    html = re.sub(
        r'(<br>\n)(<strong>[^<]+</strong>)',
        r'</p>\n<p>\2', html
    )
    return html


def normalizar_blockquotes(html):
    """Separa líneas dentro de <blockquote> en <p> individuales."""
    def procesar_bq(match):
        contenido = match.group(1)
        lineas = [l.strip() for l in contenido.split('\n') if l.strip()]
        if len(lineas) <= 1:
            return match.group(0)
        parrafos = "\n".join(f"<p>{l}</p>" for l in lineas)
        return f"<blockquote>\n{parrafos}\n</blockquote>"

    return re.sub(
        r'<blockquote>(.*?)</blockquote>',
        procesar_bq,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def normalizar_strong_sueltos(html):
    """
    FIX #2: <strong> sueltos al inicio de línea (sin <p> previo)
    → </p><p><strong> para que pandoc los separe en párrafos.
    Patrón: salto de línea seguido de <strong> sin estar dentro de <p>.
    """
    html = re.sub(
        r'(\n)(<strong>[^<]+</strong>)',
        r'\1</p><p>\2',
        html
    )
    return html


def normalizar_html(html):
    """
    Pre-procesa el HTML antes de pandoc:
    1. Saltos de línea simples → <br>
    2. Blockquotes con líneas separadas en <p>
    3. <strong> sueltos al inicio de línea → párrafos separados
    4. <code type="lang"> multilinea → <pre><code>
    5. Párrafos por \\n\\n sin <p> tags → <p>
    """
    html = normalizar_saltos_linea(html)
    html = normalizar_blockquotes(html)
    html = normalizar_strong_sueltos(html)

    # <code type="..."> multilinea → <pre><code>
    def code_a_pre(match):
        lang = match.group(1) or ""
        contenido = match.group(2)
        if "\n" in contenido:
            return f'<pre><code class="{lang}">{contenido}</code></pre>'
        return match.group(0)

    html = re.sub(
        r'<code(?:\s+type=["\']([^"\']*)["\'])?[^>]*>(.*?)</code>',
        code_a_pre,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Párrafos por \n\n sin <p> tags
    num_p = len(re.findall(r'<p[\s>]', html, re.IGNORECASE))
    num_doble_nl = len(re.findall(r'\n\s*\n', html))
    if num_doble_nl > num_p * 2:
        bloques = re.split(r'\n\s*\n', html)
        html = "\n\n".join(
            f"<p>{b.strip()}</p>" if b.strip() and not re.match(
                r'\s*<(p|h[1-6]|ul|ol|li|table|tr|td|th|pre|blockquote|div|hr)',
                b.strip(), re.IGNORECASE
            ) else b
            for b in bloques
        )

    return html


def extraer_imagenes_html(html):
    """
    Extrae <img> (con posible <a> envolvente), reescribe rutas locales
    y reemplaza por placeholders. Devuelve (html_mod, placeholders, copias_rsync).
    """
    placeholders = {}
    copias_rsync = []
    contador = [0]

    patron_img = re.compile(
        r'(<a\s[^>]*>)?\s*(<img\s[^>]*/?>)\s*(</a>)?',
        re.IGNORECASE | re.DOTALL
    )

    def reemplazar(match):
        a_open  = match.group(1) or ""
        img_tag = match.group(2)
        a_close = match.group(3) or ""
        if not img_tag:
            return match.group(0)

        def reesc_src(m):
            url = m.group(1)
            resultado = url_a_ruta_local(url)
            if resultado:
                _, origen, destino_url = resultado
                copias_rsync.append((origen, destino_url))
                return f'src="{destino_url}"'
            return m.group(0)

        img_reesc = re.sub(r'src=["\']([^"\']+)["\']', reesc_src, img_tag, flags=re.IGNORECASE)

        if a_open:
            def reesc_href(m):
                url = m.group(1)
                resultado = url_a_ruta_local(url)
                if resultado:
                    _, _, destino_url = resultado
                    return f'href="{destino_url}"'
                return m.group(0)
            a_open = re.sub(r'href=["\']([^"\']+)["\']', reesc_href, a_open, flags=re.IGNORECASE)

        bloque = a_open + img_reesc + a_close
        contador[0] += 1
        key = f"__IMG_PLACEHOLDER_{contador[0]}__"
        placeholders[key] = bloque
        return key

    return patron_img.sub(reemplazar, html), placeholders, copias_rsync


def html_a_org(html):
    """Convierte HTML a Org con pandoc."""
    html = normalizar_html(html)
    resultado = subprocess.run(
        ["pandoc", "-f", "html", "-t", "org", "--wrap=none"],
        input=html, capture_output=True, text=True, encoding="utf-8",
    )
    org = resultado.stdout
    # Eliminar bloques :PROPERTIES: que pandoc añade a headings
    org = re.sub(r"\s*:PROPERTIES:\s*\n(\s*:[A-Z_]+:.*\n)+\s*:END:", "", org)
    return org.strip()


def restaurar_imagenes(org_text, placeholders):
    """
    Sustituye placeholders de imagen por bloques #+begin_export html.
    FIX #5: Si el placeholder está precedido por un item de lista (línea
    que empieza con número + punto), indenta el bloque con 3 espacios
    para que org-mode mantenga la numeración continua.
    """
    lineas = org_text.split('\n')
    resultado = []
    patron_item_lista = re.compile(r'^\s*\d+[.):]\s')
    ultimo_item_lista = False

    for linea in lineas:
        # Detectar si hay placeholder en esta línea
        for key, bloque_html in placeholders.items():
            if key in linea:
                indent = "   " if ultimo_item_lista else ""
                export = (
                    f"\n{indent}#+begin_export html\n"
                    f"{indent}{bloque_html}\n"
                    f"{indent}#+end_export\n"
                )
                linea = linea.replace(key, export)
        resultado.append(linea)
        # Actualizar si estamos tras un item de lista numerada
        if patron_item_lista.match(linea):
            ultimo_item_lista = True
        elif linea.strip() and not linea.strip().startswith('#+'):
            ultimo_item_lista = False

    return '\n'.join(resultado)


def postprocesar_org(org_text):
    """Limpieza final del texto Org."""
    org_text = org_text.replace("<!--break-->", "")
    org_text = re.sub(r"#\+begin_html\s*\n<!--break-->\s*\n#\+end_html", "", org_text)
    # Eliminar \\ sobrantes
    org_text = re.sub(r'\\\\\s*\n', '\n', org_text)
    # Eliminar líneas en blanco excesivas
    org_text = re.sub(r"\n{3,}", "\n\n", org_text)
    return org_text.strip()


# ─── RSYNC ────────────────────────────────────────────────────────────────────

def generar_rsync(nid, copias):
    """Genera un script shell con los comandos rsync."""
    if not copias:
        return
    os.makedirs(RSYNC_DIR, exist_ok=True)
    ruta = os.path.join(RSYNC_DIR, f"rsync-{nid}.sh")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# Copia de ficheros para nodo nid={nid}\n\n")
        for origen, destino_url in copias:
            destino_local = os.path.expanduser("~/blog-estatico") + destino_url
            destino_dir   = os.path.dirname(destino_local)
            f.write(f'mkdir -p "{destino_dir}"\n')
            f.write(f'rsync -av \\\n  "{origen}" \\\n  "{destino_local}"\n\n')
    os.chmod(ruta, 0o755)
    print(f"  → rsync script: {ruta} ({len(copias)} fichero(s))")


# ─── CONSULTAS A LA BD ────────────────────────────────────────────────────────

def obtener_nodos(conn, nid=None):
    with conn.cursor() as cur:
        base = """
            SELECT
                n.nid, n.type, n.status, n.created, n.title,
                u.name AS autor,
                b.body_value, b.body_summary,
                ua.alias AS url_alias
            FROM node n
            LEFT JOIN users u ON n.uid = u.uid
            LEFT JOIN field_data_body b
                ON b.entity_id = n.nid AND b.bundle = n.type
            LEFT JOIN url_alias ua
                ON ua.source = CONCAT('node/', n.nid)
            WHERE n.type IN ({})
        """.format(", ".join(["%s"] * len(TIPOS_A_PROCESAR)))

        params = list(TIPOS_A_PROCESAR)
        if nid is not None:
            base += " AND n.nid = %s"
            params.append(nid)
        base += " ORDER BY n.created"
        cur.execute(base, params)
        return cur.fetchall()


def obtener_tags(conn, nid):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT td.name
            FROM taxonomy_index ti
            JOIN taxonomy_term_data td ON td.tid = ti.tid
            WHERE ti.nid = %s
            ORDER BY td.name
        """, (nid,))
        return [row["name"] for row in cur.fetchall()]


def obtener_comentarios(conn, nid):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.cid, c.subject, c.name, c.created,
                   cb.comment_body_value AS body
            FROM comment c
            LEFT JOIN field_data_comment_body cb ON cb.entity_id = c.cid
            WHERE c.nid = %s AND c.status = 1
            ORDER BY c.created
        """, (nid,))
        return cur.fetchall()


def obtener_adjuntos(conn, nid):
    """Devuelve adjuntos no-imagen de un nodo desde file_managed."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fm.filename, fm.uri, fm.filemime
            FROM file_managed fm
            JOIN file_usage fu ON fu.fid = fm.fid
            WHERE fu.id = %s
            AND fu.type = 'node'
            AND fm.filemime NOT LIKE 'image/%%'
            ORDER BY fm.fid
        """, (nid,))
        return cur.fetchall()


# ─── GENERACIÓN DEL FICHERO .ORG ─────────────────────────────────────────────

def escapar_guiones_bajos(texto):
    """
    FIX #4: Escapa _ en texto para evitar que org-mode los interprete
    como subíndices. Solo escapa los que están entre caracteres no espacios.
    """
    return re.sub(r'(?<=\S)_(?=\S)', r'\_', texto)


def generar_org(nodo, tags, comentarios, adjuntos):
    nid       = nodo["nid"]
    titulo    = nodo["title"]
    autor     = nodo["autor"] or "Godofredo Fdez."
    created   = nodo["created"]
    body_html = nodo["body_value"] or ""
    summary   = nodo["body_summary"] or ""

    # Fecha
    fecha_dt   = datetime.fromtimestamp(created)
    fecha_org  = fecha_dt.strftime("<%Y-%m-%d %H:%M>")
    fecha_slug = fecha_dt.strftime("%Y-%m-%d")

    # Nombre de fichero — añadir nid si hay colisión
    nombre_fichero = f"{fecha_slug}-{slugify(titulo)}.org"
    if os.path.exists(os.path.join(OUTPUT_DIR, nombre_fichero)):
        nombre_fichero = f"{fecha_slug}-{slugify(titulo)}-{nid}.org"

    # Description
    descripcion = strip_html(summary) if summary else primeras_palabras(strip_html(body_html))

    # Tags
    filetags = " ".join(t.replace(" ", "_") for t in tags) if tags else ""

    # Eliminar <!--break-->
    body_html = body_html.replace("<!--break-->", "")

    # PASO 1: Extraer bloques de código antes de pandoc (FIX #1)
    body_html, placeholders_codigo = extraer_bloques_codigo(body_html)

    # PASO 2: Extraer imágenes antes de pandoc
    body_sin_imgs, placeholders_img, copias_rsync = extraer_imagenes_html(body_html)

    # PASO 3: Convertir HTML → Org con pandoc
    body_org = html_a_org(body_sin_imgs)

    # PASO 4: Restaurar bloques de código (FIX #1)
    body_org = restaurar_bloques_codigo(body_org, placeholders_codigo)

    # PASO 5: Restaurar imágenes con indentación inteligente (FIX #5)
    body_org = restaurar_imagenes(body_org, placeholders_img)

    # PASO 6: Limpieza final
    body_org = postprocesar_org(body_org)

    # ── Adjuntos descargables (FIX #3 y #4) ──────────────────────────────
    bloque_adjuntos = ""
    if adjuntos:
        lineas = ["\n** Archivos adjuntos"]
        for adj in adjuntos:
            nombre = adj["filename"]
            uri    = adj["uri"]
            origen_local = uri_publica_a_local(uri)
            if origen_local:
                # FIX #3: URL absoluta para evitar file:///h:/ en Windows
                destino_url     = STATIC_FILES + "/" + nombre
                destino_url_abs = BLOG_BASE_URL + destino_url
                copias_rsync.append((origen_local, destino_url))
                # FIX #4: escapar _ en nombre del fichero
                nombre_escapado = escapar_guiones_bajos(nombre)
                enlace = f"- [[{destino_url_abs}][{nombre_escapado}]]"
            else:
                enlace = f"- {nombre} (ruta no resuelta: {uri})"
            lineas.append(enlace)
        bloque_adjuntos = "\n".join(lineas)

    # ── Enlace "Dejar comentario" ──────────────────────────────────────────
    titulo_encoded = quote(titulo, safe="")
    enlace_comentario = f"[[{COMMENT_FORM_BASE}{titulo_encoded}][Dejar comentario]]."

    # ── Comentarios ───────────────────────────────────────────────────────
    bloque_comentarios = ""
    if comentarios:
        lineas = ["\n** Comentarios"]
        for c in comentarios:
            subject = c["subject"] or "Sin título"
            nombre  = c["name"] or "Anónimo"
            fecha_c = datetime.fromtimestamp(c["created"]).strftime("%d/%m/%Y - %H:%M")
            cuerpo  = html_a_org(c["body"] or "") if c["body"] else ""
            cuerpo  = postprocesar_org(cuerpo)
            lineas.append(f"\n*** {subject} ({nombre} {fecha_c})")
            lineas.append(cuerpo)
        bloque_comentarios = "\n".join(lineas)

    # ── Ensamblar — FIX #4: #+OPTIONS: _:nil evita subíndices ─────────────
    contenido = (
        f"#+title: {titulo}\n"
        f"#+date: {fecha_org}\n"
        f"#+author: {autor}\n"
        f"#+description: {descripcion}\n"
        f"#+filetags: {filetags}\n"
        f"#+options: _:nil\n"
        f"\n"
        f"{body_org}\n"
        f"{bloque_adjuntos}\n"
        f"\n"
        f"{enlace_comentario}\n"
        f"{bloque_comentarios}\n"
    )

    return contenido, copias_rsync, nombre_fichero


# ─── PROGRAMA PRINCIPAL ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Migra nodos Drupal 7 a ficheros .org para org-static-blog"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--nid",  type=int, help="Procesar solo este nid")
    group.add_argument("--all",  action="store_true", help="Procesar todos los nodos")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(RSYNC_DIR, exist_ok=True)

    conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB_CONFIG)

    try:
        nid_filtro = args.nid if args.nid else None
        nodos = obtener_nodos(conn, nid=nid_filtro)

        if not nodos:
            print("No se encontraron nodos con los criterios indicados.")
            sys.exit(1)

        print(f"Nodos a procesar: {len(nodos)}\n")

        for nodo in nodos:
            nid    = nodo["nid"]
            titulo = nodo["title"]
            print(f"[nid={nid}] {titulo}")

            tags        = obtener_tags(conn, nid)
            comentarios = obtener_comentarios(conn, nid)
            adjuntos    = obtener_adjuntos(conn, nid)

            contenido, copias_rsync, nombre_fichero = generar_org(
                nodo, tags, comentarios, adjuntos
            )

            ruta_org = os.path.join(OUTPUT_DIR, nombre_fichero)
            with open(ruta_org, "w", encoding="utf-8") as f:
                f.write(contenido)
            print(f"  → org: {ruta_org}")

            if copias_rsync:
                generar_rsync(nid, copias_rsync)

        print(f"\n✓ Proceso completado. Ficheros en: {OUTPUT_DIR}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
