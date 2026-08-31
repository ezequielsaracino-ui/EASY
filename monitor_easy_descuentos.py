#!/usr/bin/env python3
"""
monitor_easy_descuentos.py

Recorre el catálogo de easy.com.ar (vía su API pública VTEX) UNA VEZ,
reporta los productos con descuento mayor al umbral configurado, los
guarda en un CSV y envía un mail si encuentra algo.

Pensado para correr en GitHub Actions con un cron cada 30 minutos
(ver .github/workflows/monitor.yml). Si lo corrés en tu PC manualmente,
también funciona: simplemente hace un ciclo y termina.

Variables de entorno necesarias para el mail (se configuran como
"Secrets" en GitHub, o como variables de entorno si lo corrés local):
    GMAIL_USER          -> tu dirección de Gmail
    GMAIL_APP_PASSWORD  -> contraseña de aplicación de 16 caracteres
    TO_EMAIL            -> (opcional) mail destino, si no se usa GMAIL_USER

--------------------------------------------------------------------------
IMPORTANTE: si el endpoint de la API cambia, revisá con las DevTools del
navegador (F12 > Network > Fetch/XHR) la URL real que usa easy.com.ar
para traer productos, y actualizá PRODUCTS_ENDPOINT / CATEGORY_TREE_ENDPOINT.
--------------------------------------------------------------------------
"""

import csv
import datetime
import os
import random
import smtplib
import sys
import time
from email.mime.text import MIMEText

import requests

# NOTA: esta es la versión "todo en uno" para correr LOCAL en tu PC:
# hace loop infinito cada INTERVALO_MINUTOS. Si estás usando la versión
# de GitHub Actions, usá el archivo separado que corre una sola vez.
INTERVALO_MINUTOS = 60

# ------------------------- CONFIGURACIÓN -------------------------

BASE_URL = "https://www.easy.com.ar"
CATEGORY_TREE_ENDPOINT = f"{BASE_URL}/api/catalog_system/pub/category/tree/3"
PRODUCTS_ENDPOINT = f"{BASE_URL}/api/catalog_system/pub/products/search"

DESCUENTO_MINIMO = 0.50
PAGE_SIZE = 50

# Si dejás esta lista vacía [], escanea TODAS las categorías (~600, tarda mucho).
# Si ponés paths acá, SOLO escanea esas (con sus subcategorías incluidas,
# porque el filtro matchea por prefijo). Sacalos de categorias.txt.
CATEGORIAS_INTERES = [
    "/banos-y-cocinas",
    "/electrodomesticos",
    "/iluminacion",
    "/pisos-y-revestimientos",
    "/pinturas",
    "/aberturas",
    "/construccion-y-maderas",
    "/plomeria",
    "/electricidad",
    "/herramientas",
    "/ferreteria",
]
DELAY_ENTRE_REQUESTS = (0.6, 1.2)   # más corto que la versión local, porque
                                     # Actions tiene un límite de tiempo por job
ARCHIVO_SALIDA = "descuentos_easy.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 8  # segundos - antes era 15, lo bajamos para no colgarnos tanto

# --------------------------------------------------------------------


def obtener_categorias():
    try:
        r = requests.get(CATEGORY_TREE_ENDPOINT, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[ERROR] No pude traer el árbol de categorías: {e}")
        return []

    categorias = []

    def recorrer(nodo):
        if nodo.get("url"):
            categorias.append(nodo["url"].replace(BASE_URL, ""))
        for hijo in nodo.get("children", []):
            recorrer(hijo)

    for nodo in data:
        recorrer(nodo)

    return categorias


def buscar_productos_categoria(categoria_path):
    productos = []
    desde = 0
    while True:
        hasta = desde + PAGE_SIZE - 1
        url = f"{PRODUCTS_ENDPOINT}{categoria_path}?_from={desde}&_to={hasta}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code in (200, 206):
                lote = r.json()
            else:
                break
        except Exception as e:
            print(f"[ERROR] Falló la request a {categoria_path}: {e}")
            break

        if not lote:
            break

        productos.extend(lote)

        if len(lote) < PAGE_SIZE:
            break

        desde += PAGE_SIZE
        time.sleep(random.uniform(*DELAY_ENTRE_REQUESTS))

        if desde > 2500:
            break

    return productos


def extraer_ofertas(productos, categoria_path):
    ofertas = []
    for p in productos:
        nombre = p.get("productName", "sin nombre")
        link = p.get("link") or p.get("linkText", "")
        for item in p.get("items", []):
            for seller in item.get("sellers", []):
                oferta = seller.get("commertialOffer", {})
                precio = oferta.get("Price")
                precio_lista = oferta.get("ListPrice")
                if not precio or not precio_lista or precio_lista <= 0:
                    continue
                descuento = 1 - (precio / precio_lista)
                if descuento >= DESCUENTO_MINIMO:
                    ofertas.append({
                        "categoria": categoria_path,
                        "producto": nombre,
                        "precio_lista": round(precio_lista, 2),
                        "precio_final": round(precio, 2),
                        "descuento_pct": round(descuento * 100, 1),
                        "link": f"{BASE_URL}{link}" if link.startswith("/") else link,
                    })
    return ofertas


def cargar_vistos():
    """
    Lee el CSV acumulado y arma un set de (link, precio_final) ya notificados.
    Esto convierte al CSV en la "base de datos" de lo que ya vimos: si un
    producto vuelve a aparecer con el MISMO precio, no se vuelve a notificar.
    Si cambia de precio (subió, bajó, o cambió el % de descuento), se
    considera nuevo y se vuelve a notificar.
    """
    vistos = set()
    if not os.path.exists(ARCHIVO_SALIDA):
        return vistos

    try:
        with open(ARCHIVO_SALIDA, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for fila in reader:
                clave = (fila.get("link", ""), fila.get("precio_final", ""))
                vistos.add(clave)
    except Exception as e:
        print(f"[AVISO] No pude leer el historial previo ({e}). Sigo sin deduplicar.")

    return vistos


def guardar_ofertas(ofertas):
    if not ofertas:
        return
    nuevo_archivo = not os.path.exists(ARCHIVO_SALIDA)

    with open(ARCHIVO_SALIDA, "a", newline="", encoding="utf-8") as f:
        campos = ["timestamp", "categoria", "producto", "precio_lista",
                  "precio_final", "descuento_pct", "link"]
        writer = csv.DictWriter(f, fieldnames=campos)
        if nuevo_archivo:
            writer.writeheader()
        ahora = datetime.datetime.now().isoformat(timespec="seconds")
        for o in ofertas:
            writer.writerow({"timestamp": ahora, **o})


def enviar_email(ofertas):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    to_email = os.environ.get("TO_EMAIL", gmail_user)

    if not gmail_user or not gmail_pass:
        print("[AVISO] No hay credenciales de Gmail configuradas (GMAIL_USER / "
              "GMAIL_APP_PASSWORD). Salteo el envío de mail.")
        return

    cuerpo = f"Encontré {len(ofertas)} producto(s) con más de {int(DESCUENTO_MINIMO*100)}% de descuento en Easy:\n\n"
    for o in ofertas:
        cuerpo += (
            f"- {o['producto']}\n"
            f"  ${o['precio_lista']} -> ${o['precio_final']} "
            f"({o['descuento_pct']}% OFF)\n"
            f"  {o['link']}\n\n"
        )

    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = f"[Easy] {len(ofertas)} oferta(s) con +{int(DESCUENTO_MINIMO*100)}% off"
    msg["From"] = gmail_user
    msg["To"] = to_email

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [to_email], msg.as_string())
        print(f"Mail enviado a {to_email}")
    except Exception as e:
        print(f"[ERROR] No se pudo enviar el mail: {e}")


def correr_ciclo():
    print(f"=== Escaneando easy.com.ar — {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")

    categorias = obtener_categorias()
    if not categorias:
        print("No se pudieron obtener categorías. Revisá el endpoint (ver comentario arriba del archivo).")
        return

    if CATEGORIAS_INTERES:
        categorias = [c for c in categorias if any(c.startswith(p) for p in CATEGORIAS_INTERES)]
        print(f"Filtro activo: {len(categorias)} categoría(s) coinciden con CATEGORIAS_INTERES.")
        if not categorias:
            print("Ninguna categoría coincidió. Revisá los paths en CATEGORIAS_INTERES contra categorias.txt.")
            return

    print(f"{len(categorias)} categorías encontradas. Buscando descuentos >= {int(DESCUENTO_MINIMO*100)}%...")

    vistos = cargar_vistos()
    print(f"Base de datos actual: {len(vistos)} oferta(s) ya notificada(s) previamente.")

    todas_las_ofertas = []
    for i, cat in enumerate(categorias, start=1):
        print(f"  [{i}/{len(categorias)}] escaneando {cat} ...", flush=True)
        productos = buscar_productos_categoria(cat)
        ofertas = extraer_ofertas(productos, cat)
        if ofertas:
            todas_las_ofertas.extend(ofertas)
        time.sleep(random.uniform(*DELAY_ENTRE_REQUESTS))

    # Filtramos: solo lo que NO estaba en el historial (nuevo, o cambió de precio)
    ofertas_nuevas = [
        o for o in todas_las_ofertas
        if (o["link"], str(o["precio_final"])) not in vistos
    ]

    print(f"Total encontradas en este ciclo: {len(todas_las_ofertas)} | Nuevas (no notificadas antes): {len(ofertas_nuevas)}")

    if ofertas_nuevas:
        print(f"¡{len(ofertas_nuevas)} oferta(s) NUEVA(S) con descuento >= {int(DESCUENTO_MINIMO*100)}%!")
        for o in ofertas_nuevas:
            print(f"  - {o['producto']} | ${o['precio_lista']} -> ${o['precio_final']} "
                  f"({o['descuento_pct']}% OFF) | {o['link']}")
        guardar_ofertas(ofertas_nuevas)
        enviar_email(ofertas_nuevas)
    else:
        print("No hay ofertas nuevas respecto al ciclo anterior (puede que sigan las mismas de antes).")


def main():
    # Si corre dentro de GitHub Actions, la variable GITHUB_ACTIONS existe:
    # en ese caso hacemos UN solo ciclo y listo (el cron de Actions se
    # encarga de repetirlo). Si estás en tu PC local, hace loop infinito.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        correr_ciclo()
        return

    print("Iniciando monitor de descuentos de easy.com.ar")
    print(f"Umbral: {int(DESCUENTO_MINIMO*100)}% | Intervalo: cada {INTERVALO_MINUTOS} min")
    print("Ctrl+C para detener.\n")
    try:
        while True:
            correr_ciclo()
            print(f"\nEsperando {INTERVALO_MINUTOS} minutos hasta el próximo escaneo...\n")
            time.sleep(INTERVALO_MINUTOS * 60)
    except KeyboardInterrupt:
        print("\nMonitor detenido por el usuario.")
        sys.exit(0)


if __name__ == "__main__":
    main()
