"""Google Sheet 공통: 서비스 계정으로 시트 열기, 탭 통째로 다시 쓰기."""
from __future__ import annotations

from .ctx import Ctx


def url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else ""


def open_sheet(ctx: Ctx, sheet_id: str):
    import gspread  # 선택 의존성: Google Sheet를 쓸 때만 필요
    return gspread.service_account(filename=ctx.cfg.google_sa_file).open_by_key(sheet_id)


def replace_tab(sh, title: str, header: list, rows: list[list]) -> None:
    """탭 내용을 header + rows로 바꿉니다 (없으면 만듦). 남는 예전 행은 지워집니다."""
    values = [header] + rows
    n = len(values) + 1  # 빈 행 하나: 모든 행을 고정(freeze)하면 Sheets API가 거부함
    ws = {w.title: w for w in sh.worksheets()}.get(title)
    if ws is None:
        ws = sh.add_worksheet(title=title, rows=n, cols=len(header))
        ws.freeze(rows=1)
    else:
        ws.clear()
        ws.resize(rows=n, cols=len(header))
    ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
