"""
Tooling to fetch Variational Omni listings and prepare for enrichment
with CoinGecko market data.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
import time
import csv
import json
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

# Variational Omni read-only API
VARI_BASE_URL: str = "https://omni-client-api.prod.ap-northeast-1.variational.io"
VARI_METADATA_STATS_PATH: str = "/metadata/stats"

# CoinGecko markets API
COINGECKO_BASE_URL: str = "https://api.coingecko.com/api/v3"
COINGECKO_COINS_MARKETS_PATH: str = "/coins/markets"

# Market data configuration
VS_CURRENCY: str = "usd"
PRICE_CHANGE_WINDOWS: str = "1h,24h,7d"
COINGECKO_MARKET_CAP_ORDER: str = "market_cap_desc"

# Optional: CoinGecko API key (for Pro if used)
COINGECKO_API_KEY_ENV: str = "COINGECKO_API_KEY"

# CoinGecko request configuration
# CoinGecko /coins/markets effectively returns up to ~100 records per call.
COINGECKO_MARKETS_PER_PAGE: int = 100
COINGECKO_SYMBOL_BATCH_SIZE: int = 50
COINGECKO_ID_BATCH_SIZE: int = 100

# Public API guidance: ~30 calls/minute (including failures).
# Keep a safety margin between calls.
COINGECKO_MIN_SECONDS_BETWEEN_CALLS: float = 2.0

OUTPUT_JSON_FILENAME: str = "listingtabledata.json"

# Hardcoded mapping: Variational ticker -> CoinGecko coin id.
# Generated from listingtabledata_ref.csv (canonical mapping).
COINGECKO_TICKER_TO_ID: Dict[str, str] = {
    "W": "wormhole",
    "PHA": "pha",
    "RAY": "raydium",
    "GIGGLE": "giggle-fund",
    "TRUMP": "official-trump",
    "ERA": "caldera",
    "AUCTION": "auction",
    "POWR": "power-ledger",
    "SPELL": "spell-token",
    "ME": "magic-eden",
    "YGG": "yield-guild-games",
    "IO": "io",
    "BLUR": "blur",
    "JUP": "jupiter-exchange-solana",
    "CETUS": "cetus-protocol",
    "CTK": "certik",
    "ALLORA": "allora",
    "POL": "polygon-ecosystem-token",
    "AAVE": "aave",
    "COTI": "coti",
    "NXPC": "nexpace",
    "ARK": "ark",
    "POPCAT": "popcat",
    "TREE": "treehouse",
    "HFT": "hashflow",
    "TWT": "trust-wallet-token",
    "AR": "arweave",
    "TAC": "tac",
    "DOGE": "dogecoin",
    "SYN": "synapse-2",
    "SWARMS": "swarms",
    "GRT": "the-graph",
    "HOME": "home",
    "CYS": "cysic",
    "ORDI": "ordinals",
    "ARCSOL": "ai-rig-complex",
    "GLM": "golem",
    "KAITO": "kaito",
    "BEL": "bella-protocol",
    "SUPER": "superfarm",
    "GWEI": "ethgas-2",
    "F": "synfutures",
    "ICNT": "impossible-cloud-network-token",
    "ON": "orochi-network",
    "API3": "api3",
    "VANATOKEN": "vana",
    "US": "talus",
    "RSR": "reserve-rights-token",
    "LSK": "lisk",
    "MUBARAK": "mubarak",
    "C98": "coin98",
    "EDGE": "edgex",
    "ESPRESSO": "espresso",
    "NAORIS": "naoris",
    "WIF": "dogwifcoin",
    "FLR": "flare-networks",
    "LIGHTER": "lighter",
    "CROSS": "cross-2",
    "CARV": "carv",
    "LINK": "chainlink",
    "KOMA": "koma-inu",
    "ASTER": "aster-2",
    "BTR0": "bitlayer-bitvm",
    "SAND": "the-sandbox",
    "CATI": "catizen",
    "IOTA": "iota",
    "MBOX": "mobox",
    "TRADOOR": "tradoor",
    "STX": "blockstack",
    "STBL": "stbl",
    "GMX": "gmx",
    "SONIC": "sonic-svm",
    "CHR": "chromaway",
    "VIRTUAL": "virtual-protocol",
    "HMSTR": "hamster-kombat",
    "1INCH": "1inch",
    "NEO": "neo",
    "UNI": "uniswap",
    "SLP": "smooth-love-potion",
    "STEEM": "steem",
    "PYTH": "pyth-network",
    "PNUT": "peanut-the-squirrel",
    "ETC": "ethereum-classic",
    "RARE": "superrare",
    "JTO": "jito-governance-token",
    "SOLV": "solv-protocol",
    "YZY": "yzy",
    "VANRY": "vanar-chain",
    "SUI": "sui",
    "XDC": "xdce-crowd-sale",
    "SYRUP": "syrup",
    "USELESS": "useless-3",
    "GRASS": "grass",
    "IMX": "immutable-x",
    "INJ": "injective-protocol",
    "LAB": "lab",
    "ONE": "harmony",
    "SXT": "space-and-time",
    "BLAST": "blast",
    "SCRT": "secret",
    "LISTA": "lista",
    "HBAR": "hedera-hashgraph",
    "CYBER": "cyberconnect",
    "ZK": "zksync",
    "MAVIA": "heroes-of-mavia",
    "ACH": "alchemy-pay",
    "LQTY": "liquity",
    "LTC": "litecoin",
    "VINE": "vine",
    "ONG": "ong",
    "PENGU": "pudgy-penguins",
    "VET": "vechain",
    "ALGO": "algorand",
    "XCN": "chain-2",
    "SNX": "havven",
    "SENTIENT": "sentient",
    "HUMIDIFI": "humidifi",
    "SUN": "sun-token",
    "IN": "infinit",
    "APRO": "apro",
    "DEEP": "deep",
    "XTZ": "tezos",
    "MNT": "mantle",
    "SKY": "sky",
    "DOT": "polkadot",
    "ONT": "ontology",
    "PYR": "vulcan-forged",
    "RECALL": "recall",
    "BABYL": "babylon",
    "HOLO": "holoworld",
    "APEX": "apex-token-2",
    "AKT": "akash-network",
    "SIGN": "sign-global",
    "MAGMA": "magma-finance",
    "RLC": "iexec-rlc",
    "ZEC": "zcash",
    "IOST": "iostoken",
    "ZBT": "zerobase",
    "DRIFT": "drift-protocol",
    "MOVR": "moonriver",
    "LAGRANGE": "lagrange",
    "ENS": "ethereum-name-service",
    "SAGA": "saga-2",
    "AXS": "axie-infinity",
    "SFP": "safepal",
    "ILV": "illuvium",
    "METEORA": "meteora",
    "GIGA": "gigachad-2",
    "UMA": "uma",
    "SAPIEN": "sapien-2",
    "GAS": "gas",
    "ZBCN": "zebec-network",
    "WLD": "worldcoin-wld",
    "UAI": "unifai-network",
    "ZKC": "boundless",
    "PUNDIX": "pundi-x-2",
    "ADA": "cardano",
    "EUL": "euler",
    "ASR": "as-roma-fan-token",
    "SCR": "scroll",
    "NEAR": "near",
    "ENA": "ethena",
    "MMT": "momentum-3",
    "GPS": "goplus-security",
    "MON": "monad",
    "GUA": "superfortune",
    "ZIL": "zilliqa",
    "QTUM": "qtum",
    "AXL": "axelar",
    "PLUME": "plume",
    "XION": "xion-2",
    "VELO": "velo",
    "KAS": "kaspa",
    "PIXEL": "pixels",
    "COW": "cow-protocol",
    "KAIA": "kaia",
    "XVG": "verge",
    "ALT": "altlayer",
    "WCT": "connect-token-wct",
    "OL": "open-loot",
    "BBIT": "bouncebit",
    "TURTLE": "turtle-4",
    "IR": "infrared-finance",
    "FLOW": "flow",
    "ATH": "aethir",
    "BARD": "lombard-protocol",
    "GALA": "gala",
    "STORJ": "storj",
    "BCH": "bitcoin-cash",
    "COOKIE": "cookie",
    "ZRX": "0x",
    "LINEA": "linea",
    "RVN": "ravencoin",
    "BRETT": "based-brett",
    "KITE": "kite-2",
    "ICP": "internet-computer",
    "THETA": "theta-token",
    "XPIN": "xpin-network",
    "XNY": "codatta",
    "ZORA": "zora",
    "SC": "siacoin",
    "CLANKER": "tokenbot-2",
    "MANTA": "manta-network",
    "DUSK": "dusk-network",
    "FLUID": "instadapp",
    "PTB": "portal-to-bitcoin",
    "STRK": "starknet",
    "4": "2-Apr",
    "TAIKO": "taiko",
    "IDOL": "meet48",
    "INIT": "initia",
    "LPT": "livepeer",
    "PONKE": "ponke",
    "MITO": "mitosis",
    "HAEDAL": "haedal",
    "ICX": "icon",
    "ENSO": "enso",
    "PARTI": "particle-network",
    "POWER": "power-protocol",
    "ZEN": "zencash",
    "DIA": "dia-data",
    "WHITEWHALE": "the-white-whale",
    "PUFFER": "puffer-finance",
    "METIS": "metis-token",
    "GNO": "gnosis",
    "TAG": "tagger",
    "HYPER": "hyperlane",
    "BEAM": "beam-2",
    "RUNE": "thorchain",
    "VELVET": "velvet",
    "NOT": "notcoin",
    "S": "sonic-3",
    "HUMA": "huma-finance",
    "VELODROME": "velodrome-finance",
    "1000BONK": "1000bonk",
    "YB": "yield-basis",
    "BROCCOLI": "czs-dog",
    "BSU": "baby-shark-universe",
    "HYPE": "hyperliquid",
    "SHELL": "myshell",
    "PROVE": "succinct",
    "ETHFI": "ether-fi",
    "REZ": "renzo",
    "ALPINE": "alpine-f1-team-fan-token",
    "TRU": "truefi",
    "BTC": "bitcoin",
    "CFX": "conflux-token",
    "ACE": "endurance",
    "DOOD": "doodles",
    "CHILLGUY": "chill-guy",
    "AGLD": "adventure-gold",
    "STG": "stargate-finance",
    "ES": "eclipse-3",
    "ACX": "across-protocol",
    "KNC": "kyber-network-crystal",
    "AERGO": "aergo",
    "SPX": "spx6900",
    "TON": "the-open-network",
    "GODS": "gods-unchained",
    "KGEN": "kgen",
    "ETH": "ethereum",
    "DOLO": "dolomite",
    "SOMI": "somnia",
    "BREV": "brevis",
    "CELO": "celo",
    "BMT": "bubblemaps",
    "IOTX": "iotex",
    "BAND": "band-protocol",
    "CGPT": "chaingpt",
    "WOO": "woo-network",
    "MOODENG": "moo-deng",
    "YFI": "yearn-finance",
    "OP": "optimism",
    "RED": "redstone-oracles",
    "KSM": "kusama",
    "AEVO": "aevo-exchange",
    "ASPECTA": "aspecta",
    "TA": "trusta-ai",
    "REQ": "request-network",
    "XRP": "ripple",
    "ARB": "arbitrum",
    "SAHARA": "sahara-ai",
    "PROMPT": "wayfinder",
    "AIOZ": "aioz-network",
    "1000FLOKI": "floki",
    "AVAX": "avalanche-2",
    "0G": "zero-gravity",
    "HIGH": "highstreet",
    "GOAT": "goatseus-maximus",
    "FET": "fetch-ai",
    "WAVES": "waves",
    "BICO": "biconomy",
    "BOME": "book-of-meme",
    "ID": "space-id",
    "TIA": "celestia",
    "B2": "bsquared-network",
    "ZRO": "layerzero",
    "1000000MOG": "mog-coin",
    "EGLD": "elrond-erd-2",
    "TRUST": "intuition",
    "KERNEL": "kernel-2",
    "XAUT": "tether-gold",
    "WLFI": "world-liberty-financial",
    "DYM": "dymension",
    "BERA": "berachain-bera",
    "BIGTIME": "big-time",
    "PEOPLE": "constitutiondao",
    "TNSR": "tensor",
    "SKL": "skale",
    "SUSHI": "sushi",
    "MANA": "decentraland",
    "BITLIGHT": "bitlight",
    "MYX": "myx-finance",
    "BR": "bedrock-token",
    "FIDA": "bonfida",
    "MAGIC": "magic",
    "BANANAS31": "banana-for-scale-2",
    "PUMPFUN": "pump-fun",
    "FARTCOIN": "fartcoin",
    "JST": "just",
    "ROAM": "roam-token",
    "TUTORIAL": "tutorial",
    "RLS": "rayls",
    "PEAQ": "peaq-2",
    "WAL": "walrus-2",
    "LDO": "lido-dao",
    "ROSE": "oasis-network",
    "PIEVERSE": "pieverse",
    "FLUX": "zelcash",
    "SOL": "solana",
    "FIO": "fio-protocol",
    "SKRS": "seeker",
    "ORCA": "orca",
    "C": "chainbase",
    "ANKR": "ankr",
    "LRC": "loopring",
    "AVNT": "avantis",
    "QNT": "quant-network",
    "A": "vaulta",
    "EIGEN": "eigenlayer",
    "ONDO": "ondo-finance",
    "LUMIA": "lumia",
    "OXT": "orchid-protocol",
    "CHZ": "chiliz",
    "SSV": "ssv-network",
    "NIL": "nillion",
    "ELSA": "elsa",
    "ORDER": "orderly-network",
    "GMT": "stepn",
    "ATOM": "cosmos",
    "CRO": "crypto-com-chain",
    "UB": "unibase",
    "XPL": "plasma",
    "BIO": "bio-protocol",
    "SANTOS": "santos-fc-fan-token",
    "ZEREBRO": "zerebro",
    "APE": "apecoin",
    "RENDER": "render-token",
    "ANIME": "anime",
    "CAMP": "camp-network",
    "ARKM": "arkham",
    "CORE": "coredaoorg",
    "CTC": "creditcoin-2",
    "HEI": "heima",
    "APT": "aptos",
    "OKB": "okb",
    "IP": "story-2",
    "SNT": "status",
    "1000PEPE": "pepe",
    "SEI": "sei-network",
    "SOPHON": "sophon",
    "SOSO": "sosovalue",
    "T": "threshold-network-token",
    "USTC": "terrausd",
    "MEW": "cat-in-a-dogs-world",
    "TAO": "bittensor",
    "BAN": "comedian",
    "FIL": "filecoin",
    "TLM": "alien-worlds",
    "ARPA": "arpa",
    "BOBA": "boba-network",
    "FOGO": "fogo",
    "TOWNS": "towns",
    "SPK": "spark-2",
    "Q": "quack-ai",
    "NMR": "numeraire",
    "NFP": "nfprompt-token",
    "ASTR": "astar",
    "MAV": "maverick-protocol",
    "MASK": "mask-network",
    "MELANIA": "melania-meme",
    "POLYX": "polymesh",
    "ESPORTS": "yooldo-games",
    "AVA": "concierge-io",
    "CC": "canton-network",
    "PENDLE": "pendle",
    "STO": "stakestone",
    "TRB": "tellor",
    "SOON": "soon-2",
    "CVC": "civic",
    "B3": "b3",
    "BCHSV": "bitcoin-cash-sv",
    "H": "humanity",
    "KAVA": "kava",
    "DBR": "debridge",
    "AIXBT": "aixbt",
    "ARIA": "aria-ai",
    "CKB": "nervos-network",
    "CVX": "convex-finance",
    "HEMI": "hemi",
    "ALCH": "alchemist-ai",
    "PORTAL": "portal-2",
    "USUAL": "usual",
    "2Z": "doublezero",
    "DASH": "dash",
    "JASMY": "jasmycoin",
    "RDNT": "radiant-capital",
    "BLESS": "bless-2",
    "TRX": "tron",
    "KMNO": "kamino",
    "XMR": "monero",
    "MERL": "merlin-chain",
    "SAFE": "safe",
    "BEAT": "audiera",
    "EDU": "edu-coin",
    "VVV": "venice-token",
    "XAN": "anoma",
    "MINA": "mina-protocol",
    "AERO": "aerodrome-finance",
    "FLOCK": "flock-2",
    "ZETA": "zetachain",
    "AZTEC": "aztec",
    "BAT": "basic-attention-token",
    "HNT": "helium",
    "AKE": "akedo",
    "RPL": "rocket-pool",
    "OG": "og-fan-token",
    "MLN": "melon",
    "RIVER": "river",
    "LUNA2": "terra-luna-2",
    "1NEIRO": "neiro-3",
    "ZAMA": "zama",
    "COMP": "compound-governance-token",
    "FF0": "falcon-finance-ff",
    "ZKJ": "polyhedra-network",
    "MORPHO": "morpho",
    "SQD": "subsquid",
    "STABLE": "stable-2",
    "AIOT": "okzoo",
    "MOVE": "movement",
    "DEXE": "dexe",
    "BASED": "based-one",
    "CAKE": "pancakeswap-token",
    "PAXG": "pax-gold",
    "OVERTAKE": "overtake",
    "BNB": "binancecoin",
    "CRV": "curve-dao-token",
    "AGI": "delysium",
    "ENJ": "enjincoin",
    "XLM": "stellar",
    "ALICE": "my-neighbor-alice",
    "IRYS": "irys",
}


def format_fetched_at_sgt() -> str:
    """Format current time in SGT as e.g. 'Fetched at: 2:41pm 3 Apr 2026 SGT'."""
    dt = datetime.now(ZoneInfo("Asia/Singapore"))
    h12 = dt.hour % 12 or 12
    ampm = dt.strftime("%p").lower()
    return (
        f"Fetched at: {h12}:{dt.minute:02d}{ampm} {dt.day} {dt.strftime('%b')} "
        f"{dt.year} SGT"
    )


def get_coingecko_headers() -> Dict[str, str]:
    """
    Build headers for CoinGecko requests, including an API key if configured.
    """
    api_key = os.getenv(COINGECKO_API_KEY_ENV)
    headers: Dict[str, str] = {}
    if api_key:
        headers["x-cg-pro-api-key"] = api_key
    return headers


def fetch_variational_metadata_stats() -> Dict[str, Any]:
    """
    Fetch the Variational Omni /metadata/stats payload.
    """
    url = f"{VARI_BASE_URL}{VARI_METADATA_STATS_PATH}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _request_json_with_retries(
    url: str,
    params: Dict[str, Any],
    headers: Dict[str, str],
    retries: int = 6,
) -> Any:
    """
    Basic retry helper for CoinGecko rate limits / transient errors.
    """
    debug = os.getenv("COINGECKO_RETRY_DEBUG", "").strip().lower() in ("1", "true", "yes", "y", "on")
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            last_status = resp.status_code
            if debug:
                retry_after_hdr = resp.headers.get("Retry-After")
                rl_headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if isinstance(k, str) and k.lower().startswith("x-ratelimit")
                }
                rl_part = f", x-ratelimit={rl_headers}" if rl_headers else ""
                print(
                    f"[CoinGecko resp] status={resp.status_code}, retry-after={retry_after_hdr}{rl_part}",
                    file=sys.stderr,
                )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait_s = float(retry_after)
                    except Exception:
                        wait_s = 5.0 + attempt * 2.0
                else:
                    wait_s = 5.0 + attempt * 2.0
                if debug:
                    print(
                        f"[CoinGecko retry] 429 rate limited (attempt {attempt+1}/{retries}); "
                        f"sleeping {max(wait_s, COINGECKO_MIN_SECONDS_BETWEEN_CALLS):.2f}s; "
                        f"params_keys={sorted(list(params.keys()))}",
                        file=sys.stderr,
                    )
                # Respect per-minute limits.
                time.sleep(max(wait_s, COINGECKO_MIN_SECONDS_BETWEEN_CALLS))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            # Backoff on transient errors.
            wait_s = max(COINGECKO_MIN_SECONDS_BETWEEN_CALLS, 1.0 + attempt * 1.0)
            if debug:
                print(
                    f"[CoinGecko retry] error {type(e).__name__} (attempt {attempt+1}/{retries}); "
                    f"sleeping {wait_s:.2f}s; msg={str(e)[:180]}",
                    file=sys.stderr,
                )
            time.sleep(wait_s)
    if last_exc is not None:
        raise RuntimeError(
            f"CoinGecko request failed after {retries} retries"
            f" (last_status={last_status}, params={params})"
        ) from last_exc
    raise RuntimeError(
        f"CoinGecko request failed after {retries} retries"
        f" (last_status={last_status}, params={params})"
    )


def _chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_coingecko_markets_for_symbols(
    symbols: List[str],
    batch_size: int = COINGECKO_SYMBOL_BATCH_SIZE,
) -> List[Dict[str, Any]]:
    """
    Fetch CoinGecko market data in symbol batches.
    CoinGecko docs: symbols lookup supports up to 50 symbols/request.
    """
    headers = get_coingecko_headers()
    all_coins: List[Dict[str, Any]] = []

    # De-duplicate and normalize symbols.
    normalized = sorted({s.strip().lower() for s in symbols if s and s.strip()})
    batches = _chunked(normalized, batch_size)

    for idx, batch in enumerate(batches):
        params: Dict[str, Any] = {
            "vs_currency": VS_CURRENCY,
            "symbols": ",".join(batch),
            "include_tokens": "top",
            "order": COINGECKO_MARKET_CAP_ORDER,
            "per_page": COINGECKO_MARKETS_PER_PAGE,
            "page": 1,
            "price_change_percentage": PRICE_CHANGE_WINDOWS,
        }
        coins = _request_json_with_retries(
            url=f"{COINGECKO_BASE_URL}{COINGECKO_COINS_MARKETS_PATH}",
            params=params,
            headers=headers,
        )
        all_coins.extend(coins or [])

        # Public API is ~30 calls/min and failed calls count too.
        # Keep request pacing conservative between batched calls.
        if idx < len(batches) - 1:
            time.sleep(COINGECKO_MIN_SECONDS_BETWEEN_CALLS)

    return all_coins


def fetch_coingecko_markets_for_ids(
    ids: List[str],
    batch_size: int = COINGECKO_ID_BATCH_SIZE,
) -> List[Dict[str, Any]]:
    """
    Fetch CoinGecko market data by explicit coin ids.
    """
    headers = get_coingecko_headers()
    all_coins: List[Dict[str, Any]] = []

    normalized = sorted({i.strip().lower() for i in ids if i and i.strip()})
    batches = _chunked(normalized, batch_size)

    for idx, batch in enumerate(batches):
        params: Dict[str, Any] = {
            "vs_currency": VS_CURRENCY,
            "ids": ",".join(batch),
            "order": COINGECKO_MARKET_CAP_ORDER,
            "per_page": COINGECKO_MARKETS_PER_PAGE,
            "price_change_percentage": PRICE_CHANGE_WINDOWS,
        }
        coins = _request_json_with_retries(
            url=f"{COINGECKO_BASE_URL}{COINGECKO_COINS_MARKETS_PATH}",
            params=params,
            headers=headers,
        )
        all_coins.extend(coins or [])

        if idx < len(batches) - 1:
            time.sleep(COINGECKO_MIN_SECONDS_BETWEEN_CALLS)

    return all_coins


def build_symbol_index(coins: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Map CoinGecko symbol (lowercased) to the "best" coin entry.
    If multiple entries share a symbol, keep the one with higher market_cap.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for coin in coins:
        sym = (coin.get("symbol") or "").lower()
        if not sym:
            continue

        if sym not in index:
            index[sym] = coin
            continue

        prev = index[sym]
        prev_cap = prev.get("market_cap")
        curr_cap = coin.get("market_cap")

        # Prefer entries with higher market cap; treat missing as 0.
        try:
            prev_cap_f = float(prev_cap) if prev_cap is not None else 0.0
        except Exception:
            prev_cap_f = 0.0
        try:
            curr_cap_f = float(curr_cap) if curr_cap is not None else 0.0
        except Exception:
            curr_cap_f = 0.0

        if curr_cap_f > prev_cap_f:
            index[sym] = coin

    return index


def build_id_index(coins: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Map CoinGecko id (lowercased) to coin entry.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for coin in coins:
        cid = (coin.get("id") or "").lower()
        if not cid:
            continue
        # Prefer entries with higher market cap on clashes.
        prev = index.get(cid)
        if prev is None:
            index[cid] = coin
            continue
        prev_cap = prev.get("market_cap")
        curr_cap = coin.get("market_cap")
        try:
            prev_cap_f = float(prev_cap) if prev_cap is not None else 0.0
        except Exception:
            prev_cap_f = 0.0
        try:
            curr_cap_f = float(curr_cap) if curr_cap is not None else 0.0
        except Exception:
            curr_cap_f = 0.0
        if curr_cap_f > prev_cap_f:
            index[cid] = coin
    return index


def _get_price_change_pct(coin: Dict[str, Any], window: str) -> Any:
    """
    window is one of: '1h', '24h', '7d'
    """
    key = f"price_change_percentage_{window}_in_currency"
    return coin.get(key)


def enrich_listings_with_coingecko(
    listings: List[Dict[str, Any]],
    symbol_index: Dict[str, Dict[str, Any]],
    id_index: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Join Variational listings to CoinGecko markets by symbol.
    """
    out_rows: List[Dict[str, Any]] = []
    mapped = 0

    for it in listings:
        vari_ticker = it.get("ticker", "")
        vari_name = it.get("name", "")
        mark_price = it.get("mark_price", "")
        vol_24h = it.get("volume_24h", "")
        oi = it.get("open_interest") or {}
        oi_long = oi.get("long_open_interest", "")
        oi_short = oi.get("short_open_interest", "")
        oi_total: Optional[float]
        try:
            oi_long_f = float(oi_long) if oi_long not in (None, "") else 0.0
            oi_short_f = float(oi_short) if oi_short not in (None, "") else 0.0
            oi_total = oi_long_f + oi_short_f
        except Exception:
            oi_total = None

        ticker_str = str(vari_ticker)
        sym = ticker_str.lower()

        # Prefer explicit id override when present from the hardcoded mapping.
        override_id = COINGECKO_TICKER_TO_ID.get(ticker_str)
        if override_id:
            coin = id_index.get(str(override_id).lower())
        else:
            coin = symbol_index.get(sym)

        if coin:
            mapped += 1

        cg_current_price = coin.get("current_price") if coin else None
        tally = "not true"
        try:
            vari_price_f = float(mark_price)
            cg_price_f = float(cg_current_price) if cg_current_price is not None else None
            if cg_price_f is not None and vari_price_f > 0:
                diff_ratio = abs(cg_price_f - vari_price_f) / vari_price_f
                if diff_ratio <= 0.05:
                    tally = "true"
        except Exception:
            tally = "not true"

        out_rows.append(
            {
                "vari_ticker": vari_ticker,
                "vari_name": vari_name,
                "mark_price": mark_price,
                 "vol_24h": vol_24h,
                "OI": oi_total,
                 "OI_long": oi_long,
                 "OI_short": oi_short,
                "coingecko_id": coin.get("id") if coin else None,
                "coingecko_symbol": coin.get("symbol") if coin else None,
                "coingecko_name": coin.get("name") if coin else None,
                "coingecko_current_price_usd": cg_current_price,
                "market_cap": coin.get("market_cap") if coin else None,
                "price_change_1h_pct": _get_price_change_pct(coin, "1h") if coin else None,
                "price_change_24h_pct": _get_price_change_pct(coin, "24h") if coin else None,
                "price_change_7d_pct": _get_price_change_pct(coin, "7d") if coin else None,
                "Tally": tally,
            }
        )

    # Attach mapped count for reporting by storing in a private key.
    for row in out_rows:
        row["_mapped"] = mapped
    return out_rows


def compute_oi_skew(oi_long: Any, oi_short: Any, oi_total: Any) -> Optional[float]:
    """
    OI Skew = |OI_long - OI_short| / OI, where OI is total open interest (long + short).
    Returns None if OI is missing or non-positive, or inputs are invalid.
    """
    try:
        oi_l = float(oi_long) if oi_long not in (None, "") else 0.0
        oi_s = float(oi_short) if oi_short not in (None, "") else 0.0
        if oi_total is None:
            return None
        denom = float(oi_total)
        if denom <= 0:
            return None
        return abs(oi_l - oi_s) / denom
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def main() -> None:
    """
    Fetches Variational stats and writes listingtabledata.txt with timestamp
    and listing summary.
    """
    stats = fetch_variational_metadata_stats()
    listings: List[Dict[str, Any]] = stats.get("listings", [])

    total_markets = stats.get("num_markets", len(listings))

    symbols: List[str] = []
    override_ids: List[str] = []
    for it in listings:
        ticker = str(it.get("ticker", ""))
        if not ticker:
            continue
        if ticker in COINGECKO_TICKER_TO_ID:
            override_ids.append(COINGECKO_TICKER_TO_ID[ticker])
        else:
            # Fallback for any new/unmapped listings.
            symbols.append(ticker.lower())

    unique_symbols_count = len(set(symbols))
    unique_override_ids_count = len(set(override_ids))
    print(
        f"Fetching CoinGecko data for {unique_symbols_count} symbols"
        f" and {unique_override_ids_count} explicit ids..."
    )
    coins_by_symbol = fetch_coingecko_markets_for_symbols(symbols)
    coins_by_id = fetch_coingecko_markets_for_ids(override_ids) if override_ids else []
    coins = coins_by_symbol + coins_by_id

    symbol_index = build_symbol_index(coins)
    id_index = build_id_index(coins)

    enriched = enrich_listings_with_coingecko(listings, symbol_index, id_index)
    mapped_count = enriched[0].get("_mapped") if enriched else 0

    def _fmt_pct(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            # CoinGecko already returns percentage points (e.g. 0.89 for 0.89%),
            # so do not multiply by 100 again.
            return f"{float(value):.2f}%"
        except Exception:
            return None

    def _sort_key(row: Dict[str, Any]) -> Any:
        ticker = str(row.get("vari_ticker", "")).upper()
        if ticker == "BTC":
            return (0, 0.0, ticker)
        if ticker == "ETH":
            return (1, 0.0, ticker)
        mcap = row.get("market_cap")
        try:
            mcap_num = float(mcap) if mcap is not None else -1.0
        except Exception:
            mcap_num = -1.0
        # For non-priority rows: descending market cap, then ticker.
        return (2, -mcap_num, ticker)

    ordered_rows = sorted(enriched, key=_sort_key)

    # Build JSON rows in the canonical format for the bot.
    json_rows: List[Dict[str, Any]] = []
    for row in ordered_rows:
        json_rows.append(
            {
                "vari_ticker": row.get("vari_ticker"),
                "vari_name": row.get("vari_name"),
                "mark_price": row.get("mark_price"),
                "vol_24h": row.get("vol_24h"),
                "OI": row.get("OI"),
                "OI_long": row.get("OI_long"),
                "OI_short": row.get("OI_short"),
                "OI Skew": compute_oi_skew(
                    row.get("OI_long"),
                    row.get("OI_short"),
                    row.get("OI"),
                ),
                "coingecko_id": row.get("coingecko_id"),
                "market_cap": row.get("market_cap"),
                "price_change_1h_pct": _fmt_pct(row.get("price_change_1h_pct")),
                "price_change_24h_pct": _fmt_pct(row.get("price_change_24h_pct")),
                "price_change_7d_pct": _fmt_pct(row.get("price_change_7d_pct")),
            }
        )

    out_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_JSON_FILENAME)
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at": format_fetched_at_sgt().replace("Fetched at: ", ""),
                "listings": json_rows,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Tickers successfully fetched: {len(listings)}")
    print(f"Wrote {out_json_path}")


if __name__ == "__main__":
    main()
