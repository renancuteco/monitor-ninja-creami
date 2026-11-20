from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import random
import re
import time
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
HISTORICO_PATH = Path("historico.json")
FUSO_BRT = timezone(timedelta(hours=-3))

# ==============================================================================
# ÁREA DE CONFIGURAÇÃO: Edite aqui os links e os preços desejados
# ==============================================================================
MONITORADOS = [
    {
        "id": "amazon_nc301_110v",
        "nome": "Ninja Creami NC301 (110V) - Amazon BR",
        "url": "https://www.amazon.com.br/dp/B08Q829H15",
        "preco_alvo": 9999.00,
    },
    {
        "id": "ml_nc301",
        "nome": "Ninja Creami NC301 - Mercado Livre",
        "url": "https://www.mercadolivre.com.br/sorveteira-ninja-creami-nc301/p/MLB27462891",
        "preco_alvo": 1550.00,
    },
    {
        "id": "oficial_nc301",
        "nome": "Ninja Creami NC301 - SharkNinja Oficial",
        "url": "https://www.ninjakitchen.com.br/sorveteira-ninja-creami-nc301/p",
        "preco_alvo": 1600.00,
    },
]
# ==============================================================================


def carregar_historico() -> dict:
    if HISTORICO_PATH.exists():
        try:
            return json.loads(HISTORICO_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def salvar_historico(historico: dict):
    HISTORICO_PATH.write_text(
        json.dumps(historico, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def enviar_telegram(mensagem: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def limpar_preco(texto: str) -> float | None:
    if not texto:
        return None
    texto = texto.strip()
    if "," in texto:
        limpo = re.sub(r"[^\d,]", "", texto).replace(",", ".")
    else:
        limpo = re.sub(r"[^\d.]", "", texto)
    try:
        valor = float(limpo)
        return valor if valor > 0 else None
    except ValueError:
        return None


def extrair_preco_json_ld(soup: BeautifulSoup) -> float | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            itens = data if isinstance(data, list) else [data]
            for item in itens:
                offers = item.get("offers")
                if isinstance(offers, dict) and "price" in offers:
                    return limpar_preco(str(offers["price"]))
                if isinstance(offers, list) and offers and "price" in offers[0]:
                    return limpar_preco(str(offers[0]["price"]))
        except Exception:
            continue
    return None


def extrair_preco_seletores(soup: BeautifulSoup, url: str) -> float | None:
    seletores = []
    if "amazon.com.br" in url:
        seletores = [
            "#corePrice_feature_div span.a-offscreen",
            "span.a-price-whole",
        ]
    elif "mercadolivre.com.br" in url:
        seletores = [
            "meta[itemprop='price']",
            ".ui-pdp-price__second-line .andes-money-amount__fraction",
        ]
    elif "magazineluiza.com.br" in url:
        seletores = ["[data-testid='price-value']"]

    for sel in seletores:
        el = soup.select_one(sel)
        if not el:
            continue
        texto = el.get("content") if el.has_attr("content") else el.get_text()
        if (
            "a-price-whole" in el.get("class", [])
            and el.find_next_sibling("span", class_="a-price-fraction")
        ):
            centavos = el.find_next_sibling(
                "span", class_="a-price-fraction"
            ).get_text()
            texto = f"{re.sub(r'[^0-9]', '', texto)},{centavos}"
        preco = limpar_preco(texto)
        if preco:
            return preco
    return None


def executar_varredura():
    historico = carregar_historico()
    agora = datetime.now(FUSO_BRT).strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()

        for item in MONITORADOS:
            item_id = item["id"]
            registro = historico.get(
                item_id,
                {
                    "nome": item["nome"],
                    "ultimo_preco": None,
                    "menor_preco_visto": None,
                    "ultimo_alerta": None,
                    "atualizado_em": None,
                },
            )

            try:
                page.goto(
                    item["url"], wait_until="domcontentloaded", timeout=30000
                )
                page.wait_for_timeout(3500)
                soup = BeautifulSoup(page.content(), "html.parser")

                preco = extrair_preco_json_ld(soup) or extrair_preco_seletores(
                    soup, item["url"]
                )

                if preco:
                    print(f"[OK] {item['nome']}: R$ {preco:.2f}")
                    menor_visto = registro.get("menor_preco_visto")
                    if menor_visto is None or preco < menor_visto:
                        registro["menor_preco_visto"] = preco

                    ultimo_alerta = registro.get("ultimo_alerta")

                    if preco <= item["preco_alvo"]:
                        if ultimo_alerta is None or preco < ultimo_alerta:
                            msg = (
                                f"🚨 *ALERTA DE PREÇO - NINJA CREAMI*\n\n"
                                f"📦 *{item['nome']}*\n"
                                f"💰 Preço atual: *R$ {preco:.2f}*\n"
                                f"🎯 Sua meta: R$ {item['preco_alvo']:.2f}\n"
                                f"📉 Menor visto: R$ {registro['menor_preco_visto']:.2f}\n\n"
                                f"🔗 [Abrir oferta agora]({item['url']})"
                            )
                            enviar_telegram(msg)
                            registro["ultimo_alerta"] = preco
                            print(f"[ALERTA ENVIADO] {item['nome']}")
                    else:
                        registro["ultimo_alerta"] = None

                    registro["ultimo_preco"] = preco
                    registro["atualizado_em"] = agora
                    historico[item_id] = registro
                else:
                    print(f"[AVISO] Preço não encontrado em {item['nome']}")

            except Exception as e:
                print(f"[ERRO] {item['nome']}: {e}")

            time.sleep(random.uniform(3, 7))

        browser.close()

    salvar_historico(historico)


if __name__ == "__main__":
    executar_varredura()
