"""Cuánto rinde comprar: venta contra alquiler del mismo edificio.

La pregunta es si conviene comprar un departamento, y la respuesta no está
en el precio sino en la relación entre lo que sale y lo que rinde. Un 2
ambientes de 150.000 USD que se alquila a 600 por mes rinde 4,8% anual; el
mismo precio con un alquiler de 400 rinde 3,2%. Ningún portal cruza las dos
cosas porque cada aviso vive en su propia búsqueda.

Dos avisos son del mismo edificio cuando su dirección geocodifica al mismo
punto. No se usa proximidad: con 10.000 avisos en diez barrios, dos
departamentos a 40 metros son vecinos casi con seguridad, no la misma
torre, y las coordenadas que publican Remax y Mudafy son aproximadas.

Eso deja afuera a las fuentes que no publican dirección (Remax y Mudafy
traen coordenadas del portal, no calle y altura), así que el cruce corre
sobre la parte de la base que sí la tiene.

El rendimiento es **bruto**: alquiler anual sobre precio de venta, sin
descontar expensas, impuestos, vacancia ni comisión. Sirve para comparar
edificios entre sí, que es para lo que está; el número final de bolsillo es
más bajo.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

# Un edificio necesita al menos esto de cada lado para que la mediana
# signifique algo. Con un solo aviso de cada uno el número existe, pero es
# el capricho de dos publicaciones y no el del edificio.
MIN_AVISOS = 1


def _lado(conn: sqlite3.Connection, etiqueta: str) -> pd.DataFrame:
    """Avisos con dirección geocodificada, resumidos por edificio.

    Se pide `address` además de las coordenadas porque es lo que distingue
    un punto del geocodificador —exacto y repetible para la misma calle y
    altura— de las coordenadas aproximadas que publica el portal.
    """
    df = pd.read_sql_query(
        """SELECT latitude, longitude, address, url, rooms, price_norm,
                  COALESCE(covered_area, total_area) AS area
             FROM listings
            WHERE active = 1
              AND latitude IS NOT NULL
              AND address IS NOT NULL AND TRIM(address) <> ''
              AND price_norm > 0""",
        conn,
    )
    if df.empty:
        return df

    df = df[df["area"] > 0].copy()
    df["punto"] = list(zip(df["latitude"].round(6), df["longitude"].round(6)))
    df["por_m2"] = df["price_norm"] / df["area"]

    agrupado = df.groupby("punto").agg(
        **{
            f"{etiqueta}_por_m2": ("por_m2", "median"),
            f"{etiqueta}_precio": ("price_norm", "median"),
            f"{etiqueta}_avisos": ("price_norm", "size"),
            f"{etiqueta}_ambientes": ("rooms", "median"),
            # Un aviso cualquiera del edificio, para poder abrirlo y
            # comprobar que la dirección es la que decimos. Un cruce que no
            # se puede auditar hay que creerlo, y no es la idea.
            f"{etiqueta}_url": ("url", "first"),
            "latitude": ("latitude", "first"),
            "longitude": ("longitude", "first"),
            "address": ("address", "first"),
        }
    )
    return agrupado


def rental_yields(
    ventas: sqlite3.Connection,
    alquileres: sqlite3.Connection,
    min_avisos: int = MIN_AVISOS,
) -> pd.DataFrame:
    """Rendimiento bruto anual por edificio, de mayor a menor.

    Se compara **por m²** y no por aviso: así un monoambiente en alquiler
    sirve para medir un 3 ambientes en venta del mismo edificio, que es lo
    normal — casi nunca hay las dos cosas en la misma tipología.
    """
    v = _lado(ventas, "venta")
    a = _lado(alquileres, "alquiler")
    if v.empty or a.empty:
        return pd.DataFrame()

    juntos = v.join(a.drop(columns=["latitude", "longitude", "address"]), how="inner")
    juntos = juntos[
        (juntos["venta_avisos"] >= min_avisos) & (juntos["alquiler_avisos"] >= min_avisos)
    ]
    if juntos.empty:
        return juntos

    juntos["rendimiento_pct"] = (
        100 * juntos["alquiler_por_m2"] * 12 / juntos["venta_por_m2"]
    )
    # Cuántos años de alquiler paga la compra. Es el mismo dato dado vuelta,
    # pero se entiende sin pensar: 25 años es caro, 15 es barato.
    juntos["años_para_pagarlo"] = 100 / juntos["rendimiento_pct"]

    return juntos.sort_values("rendimiento_pct", ascending=False).reset_index()
