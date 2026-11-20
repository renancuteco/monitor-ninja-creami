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
# SEUS LINKS MONITORADOS (Ajuste o "preco_alvo" conforme desejar)
# ==============================================================================
MONITORADOS = [
    {
        "id": "sharkninja_oficial_110v",
        "nome": "Ninja Creami (110V) - SharkNinja Oficial",
        "url": "https://www.sharkninjabrasil.com.br/ninja-creami-sorveteira-110v/p",
        "preco_alvo": 1550.00,
    },
    {
        "id": "fastshop_110v",
        "nome": "Ninja Creami (110V) - Fast Shop",
        "url": "https://site.fastshop.com.br/sorveteira-creami-110v-ninja-148297-157979/p",
        "preco_alvo": 1550.00,
    },
    {
        "id": "amazon_nc301",
        "nome": "Ninja Creami - Amazon BR",
        "url": "https://www.amazon.com.br/dp/B0DHSWL7D6",
        "preco_alvo": 1550.00,
    },
    {
        "id": "ml_110v",
        "nome": "Ninja Creami (110V) - Mercado Livre",
        "url": "https://www.mercadolivre.com.br/sorveteira-ninja-creami-7-em-1-tecnologia-creamify-110-v-2-potes-auto-iq-sorvete-gelato-sorbet-milkshake-e-smoothie-bowl/p/MLB22342330",
        "url_busca_ml": "https://lista.mercadolivre.com.br/sorveteira-ninja-creami-110v",
        "voltagem": "110",
        "preco_alvo": 1550.00,
    },
    {
        "id": "ml_220v",
        "nome": "Ninja Creami (220V) - Mercado Livre",
        "url": "https://www.mercadolivre.com.br/sorveteira-7-em-1-ninja-creami-220-v-2-potes-inclusos-auto-iq-7-programas-sorvete-sorvete-light-sorbet-gelato-milkshake-smoothie-bowl-e-misturar/p/MLB47221332",
        "url_busca_ml": "https://lista.mercadolivre.com.br/sorveteira-ninja-creami-220v",
        "voltagem": "220",
        "preco_alvo": 1550.00,
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
        return valor if valor > 900 else None  # Ignora parcelas e acessórios avulsos
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
                if isinstance(offers, dict):
                    for chave in ("price", "lowPrice"):
                        if offers.get(chave):
                            preco = limpar_preco(str(offers[chave]))
                            if preco:
                                return preco
                    sub_offers = offers.get("offers")
                    if isinstance(sub_offers, list) and sub_offers:
                        preco = limpar_preco(str(sub_offers[0].get("price", "")))
                        if preco:
                            return preco
                elif isinstance(offers, list) and offers:
                    preco = limpar_preco(str(offers[0].get("price", "")))
                    if preco:
                        return preco
        except Exception:
            continue
    return None


def extrair_preco_seletores(soup: BeautifulSoup, url: str) -> float | None:
    seletores = [
        "meta[property='product:price:amount']",
        "meta[property='og:price:amount']",
        "meta[itemprop='price']",
    ]
    if "amazon.com.br" in url:
        seletores += [
            "#corePrice_feature_div span.a-offscreen",
            ".apexPriceToPay span.a-offscreen",
            "span.a-price-whole",
        ]
    elif "mercadolivre.com.br" in url:
        seletores += [
            ".ui-pdp-price__second-line .andes-money-amount__fraction",
            ".andes-money-amount__fraction",
        ]
    elif "sharkninjabrasil.com.br" in url or "fastshop.com.br" in url:
        seletores += [
            "[class*='sellingPriceValue']",
            "[class*='spotPriceValue']",
        ]

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


def extrair_menor_preco_busca_ml(
    soup: BeautifulSoup, voltagem: str
) -> tuple[float | None, str | None]:
    """Lê os resultados da página de busca do ML filtrando acessórios e voltagem."""
    palavras_proibidas = ("pote", "copo", "tampa", "lamina", "lâmina", "usado", "tigela")
    melhor_preco = None
    melhor_link = None

    for card in soup.select(".ui-search-layout__item, .poly-card"):
        texto_card = card.get_text(" ", strip=True).lower()
        if "creami" not in texto_card:
            continue
        if any(p in texto_card for p in palavras_proibidas):
            continue
        # Se o anúncio especificar a outra voltagem, ignora
        outra_voltagem = "220" if voltagem == "110" else "110"
        if f"{outra_voltagem}v" in texto_card or f"{outra_voltagem} v" in texto_card:
            continue

        preco_el = card.select_one(
            ".poly-price__current .andes-money-amount__fraction, .ui-search-price__second-line .andes-money-amount__fraction"
        )
        if not preco_el:
            continue

        preco = limpar_preco(preco_el.get_text())
        if preco and (melhor_preco is None or preco < melhor_preco):
            melhor_preco = preco
            link_el = card.select_one("a[href]")
            if link_el:
                melhor_link = link_el["href"].split("#")[0]

    return melhor_preco, melhor_link


def executar_varredura():
    historico = carregar_historico()
    agora = datetime.now(FUSO_BRT).strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Contexto Desktop (Amazon, Fast Shop, SharkNinja)
        context_desktop = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 768},
        )
        context_desktop.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Contexto Mobile Android (Mercado Livre)
        context_mobile = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Mobile Safari/537.36"
            ),
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 412, "height": 915},
            is_mobile=True,
        )
        context_mobile.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page_desktop = context_desktop.new_page()
        page_mobile = context_mobile.new_page()

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

            eh_ml = "mercadolivre.com.br" in item["url"]
            page = page_mobile if eh_ml else page_desktop
            link_oferta = item["url"]

            try:
                page.goto(
                    item["url"], wait_until="domcontentloaded", timeout=60000
                )
                page.wait_for_timeout(4500)
                soup = BeautifulSoup(page.content(), "html.parser")

                preco = extrair_preco_json_ld(soup) or extrair_preco_seletores(
                    soup, item["url"]
                )

                # Fallback para página de busca se o ML bloquear o link direto
                if not preco and eh_ml and item.get("url_busca_ml"):
                    page.goto(
                        item["url_busca_ml"],
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    page.wait_for_timeout(4500)
                    soup_busca = BeautifulSoup(page.content(), "html.parser")
                    preco, link_encontrado = extrair_menor_preco_busca_ml(
                        soup_busca, item.get("voltagem", "110")
                    )
                    if link_encontrado:
                        link_oferta = link_encontrado
                    soup = soup_busca

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
                                f"🔗 [Abrir oferta agora]({link_oferta})"
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
                    titulo = (
                        soup.title.string.strip()
                        if soup.title and soup.title.string
                        else "Sem título"
                    )
                    print(
                        f"[AVISO] Preço não encontrado em {item['nome']} | Página carregada: {titulo}"
                    )

            except Exception as e:
                print(f"[ERRO] {item['nome']}: {e}")

            time.sleep(random.uniform(3, 7))

        browser.close()

    salvar_historico(historico)


if __name__ == "__main__":
    executar_varredura()
