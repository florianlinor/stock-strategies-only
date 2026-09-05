"""三大法人（外資／投信／自營商）籌碼濾網。

不動 evaluate() 的核心評分，只在 evaluate() 產出結果之後，做一層與大盤濾鏡、
夜盤濾鏡同樣模式的「後製」：
- 幫每檔結果附上近 N 日三大法人合計買賣超（張）與連續賣超天數。
- 若 BUY 訊號遇上法人連續賣超達到門檻，自動降級為 WATCH，並附註原因
  （寫法與 market.apply_market_filter / night_session.apply_night_filter 一致）。
"""
from datetime import datetime, timedelta

import pandas as pd

from .cache import fetch_finmind_cached

INSTITUTIONAL_CATEGORIES = [
    "Foreign_Investor", "Foreign_Dealer_Self",
    "Investment_Trust", "Dealer_self", "Dealer_Hedging",
]


def get_institutional_daily_net(stock_id: str, days: int = 20) -> pd.Series:
    """回傳近 `days` 個交易日、三大法人合計買賣超（單位：股），依日期由舊到新排序。"""
    start = (datetime.now() - timedelta(days=days * 3 + 10)).strftime("%Y-%m-%d")
    df = fetch_finmind_cached("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start)
    if df.empty or "name" not in df.columns:
        return pd.Series(dtype=float)

    df = df[df["name"].isin(INSTITUTIONAL_CATEGORIES)].copy()
    if df.empty:
        return pd.Series(dtype=float)

    df["buy"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0)
    df["sell"] = pd.to_numeric(df["sell"], errors="coerce").fillna(0)
    df["net"] = df["buy"] - df["sell"]

    daily = df.groupby("date")["net"].sum().sort_index()
    return daily.tail(days)


def _sell_streak(daily_net: pd.Series) -> int:
    """從最新一天往回數，連續「合計淨賣超」的天數。"""
    streak = 0
    for v in reversed(daily_net.tolist()):
        if v < 0:
            streak += 1
        else:
            break
    return streak


def apply_chips_filter(
    results: list[dict],
    net_window: int = 5,
    downgrade_streak: int = 3,
) -> int:
    """幫每檔結果附上法人籌碼資訊；BUY 若遇法人連續賣超達門檻則降為 WATCH。

    回傳被降級的檔數。查詢失敗的個股（例如資料不足）不影響原本的 action，
    僅略過附註，不會讓整支程式中斷。
    """
    downgraded = 0
    for r in results:
        if r.get("action") not in ("BUY", "WATCH"):
            continue
        stock_id = str(r.get("stock_id", ""))
        if not stock_id:
            continue
        try:
            daily_net = get_institutional_daily_net(stock_id, days=max(net_window, downgrade_streak) + 5)
        except Exception:
            continue
        if daily_net.empty:
            continue

        net_recent = float(daily_net.tail(net_window).sum())
        streak = _sell_streak(daily_net)

        r.setdefault("components", {})["institutional_net_5d"] = round(net_recent / 1000, 0)  # 股 -> 張
        r["components"]["institutional_sell_streak"] = streak

        if streak >= downgrade_streak:
            note = f"三大法人連續賣超 {streak} 天，追高風險"
            if r["action"] == "BUY":
                r["action"] = "WATCH"
                downgraded += 1
            r.setdefault("risk_notes", []).append(note)
        elif net_recent > 0:
            r.setdefault("risk_notes", []).append(f"法人近{net_window}日買超 {net_recent/1000:,.0f} 張")

    return downgraded
