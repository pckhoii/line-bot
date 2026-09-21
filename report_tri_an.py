"""Read the tri-an report inputs from Google Sheets and build a LINE summary.

The report intentionally uses a service account with read-only access. It does
not make any change to the source workbook.
"""

from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build


class ReportDataError(ValueError):
    """A source sheet is missing a required tab, header, or usable data."""


@dataclass(frozen=True)
class TriAnReport:
    report_date: pd.Timestamp
    summary: str


ORIGINS = ("NỘI ĐỊA", "NHẬP KHẨU")
SHEETS_SCOPE = ("https://www.googleapis.com/auth/spreadsheets.readonly",)


def build_tri_an_summary(requested_date: str | None = None) -> TriAnReport:
    spreadsheet_id = required_env("GOOGLE_SHEET_ID")
    service = sheets_service()
    sales = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_SALES_TAB", "so_ban"))
    inventory = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_INVENTORY_TAB", "so_ton"))
    stores = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_STORES_TAB", "sieu_thi"))
    products = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_PRODUCTS_TAB", "sp"))

    stores = prepare_stores(stores)
    products = prepare_products(products)
    inventory, report_date = prepare_inventory(inventory, requested_date)
    sales = prepare_sales(sales)
    store_ids = set(stores["Mã siêu thị"]) - {""}
    model_ids = set(products["Mã Model"]) - {""}
    inventory = inventory[(inventory["Ngày"] == report_date) & inventory["Mã siêu thị"].isin(store_ids) & inventory["Mã Model"].isin(model_ids)]
    sales = sales[(sales["Ngày"] == report_date) & sales["Mã siêu thị"].isin(store_ids) & sales["Mã Model"].isin(model_ids)]

    inventory = inventory.merge(products, on="Mã Model", how="inner")
    sales = sales.merge(products, on="Mã Model", how="inner")
    stock_by_store = inventory.groupby(["Mã siêu thị", "Loại NCC"], as_index=False)["Tồn kho"].sum().rename(columns={"Tồn kho": "Số tồn"})
    sold_by_store = sales.groupby(["Mã siêu thị", "Loại NCC"], as_index=False)["Số bán"].sum()
    base = cross_join_stores_and_origins(stores)
    base = base.merge(stock_by_store, on=["Mã siêu thị", "Loại NCC"], how="left")
    base = base.merge(sold_by_store, on=["Mã siêu thị", "Loại NCC"], how="left")
    base[["Số tồn", "Số bán"]] = base[["Số tồn", "Số bán"]].fillna(0)
    return TriAnReport(report_date=report_date, summary=format_summary(base, report_date))


def sheets_service():
    encoded_key = required_env("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    try:
        key_info = json.loads(base64.b64decode(encoded_key).decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        raise ReportDataError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 không phải JSON key Base64 hợp lệ.") from error
    credentials = service_account.Credentials.from_service_account_info(key_info, scopes=SHEETS_SCOPE)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_table(service: Any, spreadsheet_id: str, tab_name: str) -> pd.DataFrame:
    try:
        response = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=quoted_tab_range(tab_name)).execute()
    except Exception as error:  # noqa: BLE001
        raise ReportDataError(f"Không đọc được tab '{tab_name}'. Kiểm tra tab tồn tại và đã share Sheet cho Service Account quyền Viewer.") from error
    values = response.get("values", [])
    if len(values) < 2:
        raise ReportDataError(f"Tab '{tab_name}' chưa có dữ liệu hoặc thiếu hàng tiêu đề.")
    headers = make_unique_headers([str(value).strip() for value in values[0]])
    rows = [row + [""] * (len(headers) - len(row)) for row in values[1:]]
    return pd.DataFrame(rows, columns=headers)


def make_unique_headers(headers: list[str]) -> list[str]:
    """Match pandas' ``.1`` convention for duplicate Google Sheets headers."""
    seen: dict[str, int] = {}
    unique: list[str] = []
    for header in headers:
        count = seen.get(header, 0)
        unique.append(header if count == 0 else f"{header}.{count}")
        seen[header] = count + 1
    return unique


def quoted_tab_range(tab_name: str) -> str:
    return "'" + tab_name.replace("'", "''") + "'"


def prepare_stores(frame: pd.DataFrame) -> pd.DataFrame:
    frame = select_and_rename(frame, {"Mã siêu thị": ("Siêu thị tính sức bán", "Mã siêu thị"), "Tên siêu thị": ("Siêu thị tính sức bán.1", "Tên siêu thị"), "Miền": ("Miền",), "RSM": ("RSM",), "QLTP": ("QLTP",)}, "sieu_thi")
    for column in frame.columns:
        frame[column] = clean_text(frame[column])
    return frame.drop_duplicates("Mã siêu thị")


def prepare_products(frame: pd.DataFrame) -> pd.DataFrame:
    frame = select_and_rename(frame, {"Mã Model": ("Mã Model",), "Mã sản phẩm": ("Mã sản phẩm", "Mã hàng"), "Tên sản phẩm": ("Tên sản phẩm", "Tên hàng"), "Loại NCC": ("LOẠI NCC", "Loại NCC")}, "sp")
    frame["Mã Model"] = clean_id(frame["Mã Model"])
    frame["Mã sản phẩm"] = clean_id(frame["Mã sản phẩm"])
    frame["Tên sản phẩm"] = clean_text(frame["Tên sản phẩm"])
    frame["Loại NCC"] = normalize_origin(frame["Loại NCC"])
    frame = frame[frame["Loại NCC"].isin(ORIGINS)]
    if frame.empty:
        raise ReportDataError("Tab 'sp' không có sản phẩm loại NCC NỘI ĐỊA hoặc NHẬP KHẨU.")
    return frame.drop_duplicates("Mã Model")


def prepare_inventory(frame: pd.DataFrame, requested_date: str | None) -> tuple[pd.DataFrame, pd.Timestamp]:
    frame = select_and_rename(frame, {"Ngày nguồn": ("Tồn đầu ngày", "Ngày"), "Mã siêu thị": ("Mã siêu thị",), "Mã Model": ("Mã Model",), "Tồn kho": ("Tồn kho siêu thị", "Số tồn", "Tồn kho")}, "so_ton")
    frame["Ngày"] = parse_date(frame["Ngày nguồn"])
    report_date = resolve_report_date(requested_date, frame["Ngày"])
    frame["Mã siêu thị"] = clean_id(frame["Mã siêu thị"])
    frame["Mã Model"] = clean_id(frame["Mã Model"])
    frame["Tồn kho"] = positive_number(frame["Tồn kho"])
    return frame, report_date


def prepare_sales(frame: pd.DataFrame) -> pd.DataFrame:
    frame = select_and_rename(frame, {"Ngày nguồn": ("Ngày",), "Mã siêu thị nguồn": ("Mã siêu thị",), "Mã Model": ("Mã Model",), "Tổng số lượng": ("Tổng số lượng",), "SL xuất km": ("SL xuất km",)}, "so_ban")
    frame["Ngày"] = parse_date(frame["Ngày nguồn"])
    frame["Mã siêu thị"] = clean_id(frame["Mã siêu thị nguồn"].astype("string").str.split("-", n=1).str[0])
    frame["Mã Model"] = clean_id(frame["Mã Model"])
    frame["Số bán"] = positive_number(frame["Tổng số lượng"]) + positive_number(frame["SL xuất km"])
    return frame


def select_and_rename(frame: pd.DataFrame, mapping: dict[str, tuple[str, ...]], tab_name: str) -> pd.DataFrame:
    normalized = {normalize_header(column): column for column in frame.columns}
    selected: dict[str, pd.Series] = {}
    missing: list[str] = []
    for destination, aliases in mapping.items():
        source = next((normalized.get(normalize_header(alias)) for alias in aliases if normalize_header(alias) in normalized), None)
        if source is None:
            missing.append(" / ".join(aliases))
        else:
            selected[destination] = frame[source]
    if missing:
        raise ReportDataError(f"Tab '{tab_name}' thiếu cột: {', '.join(missing)}.")
    return pd.DataFrame(selected)


def normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFD", str(value).strip().casefold())
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", normalized.replace("đ", "d"))


def clean_id(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip().str.replace(r"\.0$", "", regex=True)


def clean_text(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip().str.replace(r"\s+", " ", regex=True)


def normalize_origin(series: pd.Series) -> pd.Series:
    value = clean_text(series).str.upper()
    return value.mask(value.str.contains("NHẬP", na=False), "NHẬP KHẨU").mask(value.str.contains("NỘI", na=False), "NỘI ĐỊA")


def parse_date(series: pd.Series) -> pd.Series:
    text = clean_id(series)
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    return parsed.fillna(pd.to_datetime(series, errors="coerce")).dt.normalize()


def resolve_report_date(value: str | None, dates: pd.Series) -> pd.Timestamp:
    if value:
        try:
            return pd.Timestamp(pd.to_datetime(value, errors="raise")).normalize()
        except (TypeError, ValueError) as error:
            raise ReportDataError("Ngày báo cáo phải theo dạng YYYY-MM-DD, ví dụ 2026-09-15.") from error
    available = dates.dropna()
    if available.empty:
        raise ReportDataError("Tab 'so_ton' không có ngày tồn kho hợp lệ.")
    return pd.Timestamp(available.max()).normalize()


def positive_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0).clip(lower=0)


def cross_join_stores_and_origins(stores: pd.DataFrame) -> pd.DataFrame:
    return stores.assign(_key=1).merge(pd.DataFrame({"Loại NCC": ORIGINS, "_key": 1}), on="_key").drop(columns="_key")


def format_summary(base: pd.DataFrame, report_date: pd.Timestamp) -> str:
    total_stores = base["Mã siêu thị"].nunique()
    totals = base.groupby("Loại NCC", as_index=False)[["Số tồn", "Số bán"]].sum()
    regional = base.groupby(["Loại NCC", "Miền"], as_index=False).agg(SLST=("Mã siêu thị", "nunique"), **{"Số tồn": ("Số tồn", "sum"), "Số bán": ("Số bán", "sum")}).sort_values(["Loại NCC", "Số bán"], ascending=[True, False])
    lines = [f"Báo cáo tri ân ngày {report_date:%d/%m/%Y}", f"Phạm vi: {total_stores:,} siêu thị.", "", "Tổng quan:"]
    for _, row in totals.iterrows():
        stock = float(row["Số tồn"])
        sold = float(row["Số bán"])
        ratio = sold / stock if stock else 0
        lines.append(
            f"- {row['Loại NCC']}: tồn {stock:,.0f}; bán {sold:,.0f}; bán/tồn {ratio:.1%}."
        )
    lines.extend(["", "Theo miền:"])
    for _, row in regional.iterrows():
        stock = float(row["Số tồn"])
        sold = float(row["Số bán"])
        ratio = sold / stock if stock else 0
        lines.append(
            f"- {row['Loại NCC']} | {row['Miền']}: {int(row['SLST']):,} ST, "
            f"tồn {stock:,.0f}, bán {sold:,.0f}, {ratio:.1%}."
        )
    return "\n".join(lines)[:4900]


def env_tab(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ReportDataError(f"Thiếu biến Railway: {name}.")
    return value
