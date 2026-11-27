"""Excel and PDF report generators with full off-loop rendering (MEDIUM-11) and completeness flags."""

from __future__ import annotations

import asyncio
import datetime
import re
import uuid
from pathlib import Path

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from sqlalchemy import desc, func, select

from config import BOT_TZ, EXPORT_DIR
from database import crud
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    GAS_METERS,
    IRRIGATION_METERS,
    IRRIGATION_MODE_AFTER_COLD,
    METER_PLAIN_LABELS,
    METER_UNITS,
    MeterReading,
    WATER_METERS,
    format_property_short,
    format_property_title,
)
from services.calculations import calculate_billing, fmt_date, fmt_num

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
EXCEL_HISTORY_LIMIT = 500
PDF_HISTORY_LIMIT = 40


def _fmt_opt(val) -> str:
    return "—" if val is None else str(val)


def _safe_sheet_title(raw: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\/*?:\[\]]", "_", raw).strip() or "Объект"
    base = cleaned[:31]
    if base not in used:
        used.add(base)
        return base
    idx = 2
    while True:
        suffix = f" ({idx})"
        candidate = f"{cleaned[:31 - len(suffix)]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        idx += 1


async def _collect_export_snapshots(apartments: list, history_limit: int) -> list[dict]:
    snapshots: list[dict] = []
    for apt in apartments:
        billing = await calculate_billing(apt.id)
        async with crud.UnitOfWork() as s:
            cnt_res = await s.execute(
                select(func.count(MeterReading.id)).where(MeterReading.apartment_id == apt.id)
            )
            total_count = int(cnt_res.scalar() or 0)
            rows_res = await s.execute(
                select(MeterReading)
                .where(MeterReading.apartment_id == apt.id)
                .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
                .limit(history_limit)
            )
            readings = list(rows_res.scalars().all())
        snapshots.append({
            "short": format_property_short(apt),
            "title": format_property_title(apt),
            "agreement_no": apt.agreement_no or "—",
            "agreement_start": fmt_date(apt.agreement_date_start),
            "agreement_finish": fmt_date(apt.agreement_date_finish),
            "billing": billing,
            "total_count": total_count,
            "readings": [
                {
                    "id": r.id,
                    "date": r.reading_date.strftime("%d.%m.%Y"),
                    "meter_type": r.meter_type,
                    "value": r.value,
                    "submitted_by": r.submitted_by,
                    "is_replacement_init": bool(getattr(r, "is_replacement_init", False)),
                }
                for r in readings
            ],
        })
    return snapshots


def _build_excel_sync(snapshots: list[dict], filepath: Path) -> Path:
    wb = Workbook()
    first = True
    used_titles: set[str] = set()
    header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    bold_font = Font(bold=True)

    for snap in snapshots:
        title = _safe_sheet_title(snap["short"], used_titles)
        ws = wb.active if first else wb.create_sheet(title=title)
        ws.title = title
        first = False

        ws.append([
            snap["title"],
            f"Договор: {snap['agreement_no']}",
            f"Срок: {snap['agreement_start']} — {snap['agreement_finish']}",
        ])
        ws["A1"].font = bold_font
        ws.append([])

        billing = snap["billing"]
        status_note = (
            ""
            if billing.is_complete
            else f" (предварительный: {', '.join(billing.incomplete_reasons)})"
        )
        ws.append(["Текущий расчёт", f"{billing.period_label}{status_note}"])
        ws[f"A{ws.max_row}"].font = bold_font
        ws.append(["Счётчик", "Предыдущее", "Текущее", "Расход", "Ед.", "Тариф (₽)", "Сумма (₽)"])
        for cell in ws[ws.max_row]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        is_irr_after_cold = (
            billing.irrigation_mode == IRRIGATION_MODE_AFTER_COLD
            and any(m in billing.meters for m in IRRIGATION_METERS)
        )
        for mt in ALL_METERS:
            m = billing.meters.get(mt)
            if m:
                if mt in IRRIGATION_METERS and is_irr_after_cold:
                    tariff_cell = "по тарифу ХВС"
                    cost_cell = "в составе ХВС"
                else:
                    tariff_cell = float(m.tariff) if m.tariff is not None else "Не задан"
                    cost_cell = m.cost if (m.has_base and m.has_tariff and m.submitted) else "—"
                ws.append([
                    METER_PLAIN_LABELS[mt],
                    _fmt_opt(m.previous),
                    _fmt_opt(m.current) if m.submitted else "Не подано",
                    m.consumption,
                    METER_UNITS[mt],
                    tariff_cell,
                    cost_cell,
                ])

        if billing.sewerage:
            sew = billing.sewerage
            has_gvs = "hot_water" in billing.meters
            sew_label = (
                "Водоотведение (ХВС + ГВС минус Полив)" if (is_irr_after_cold and has_gvs)
                else "Водоотведение (ХВС минус Полив)" if is_irr_after_cold
                else "Водоотведение (ХВС + ГВС)"
            )
            ws.append([
                sew_label,
                "—",
                "—" if sew.submitted else "Не подано",
                sew.consumption,
                "м³",
                float(sew.tariff) if sew.tariff is not None else "Не задан",
                sew.cost if (sew.has_base and sew.has_tariff and sew.submitted) else "—",
            ])

        if any(m in billing.meters for m in WATER_METERS):
            ws.append(["Итого вода", "", "", billing.water_consumption_total, "м³", "", billing.water_total])
        if billing.sewerage:
            ws.append(["Итого водоотведение", "", "", billing.sewerage_consumption_total, "м³", "", billing.sewerage_total])
        if any(m in billing.meters for m in IRRIGATION_METERS):
            if is_irr_after_cold:
                ws.append(["Полив (вычет из водоотведения)", "", "", billing.irrigation_consumption_total, "м³", "", "в ХВС"])
            else:
                ws.append(["Итого полив", "", "", billing.irrigation_consumption_total, "м³", "", billing.irrigation_total])
        if any(m in billing.meters for m in ELECTRICITY_METERS):
            ws.append(["Итого электричество", "", "", billing.electricity_consumption_total, "кВт·ч", "", billing.electricity_total])
        if any(m in billing.meters for m in GAS_METERS):
            ws.append(["Итого газ", "", "", billing.gas_consumption_total, "м³", "", billing.gas_total])

        total_label = "ВСЕГО К ОПЛАТЕ" if billing.is_complete else "ВСЕГО К ОПЛАТЕ (предварительно)"
        ws.append([total_label, "", "", "", "", "", billing.grand_total])
        ws[f"A{ws.max_row}"].font = bold_font
        ws[f"G{ws.max_row}"].font = bold_font
        ws.append([])

        readings = snap["readings"]
        total_count = snap["total_count"]
        hist_title = (
            f"История показаний (показаны последние {len(readings)} из {total_count})"
            if total_count > len(readings)
            else f"История показаний (всего {total_count})"
        )
        ws.append([hist_title])
        ws[f"A{ws.max_row}"].font = bold_font
        ws.append(["ID", "Дата", "Счётчик", "Значение", "Ед.", "Кем подано (TG ID)", "Примечание"])
        for cell in ws[ws.max_row]:
            cell.fill = header_fill
            cell.font = header_font

        for r in readings:
            ws.append([
                r["id"],
                r["date"],
                METER_PLAIN_LABELS.get(r["meter_type"], r["meter_type"]),
                r["value"],
                METER_UNITS.get(r["meter_type"], ""),
                r["submitted_by"],
                "Начальное (замена счётчика)" if r.get("is_replacement_init") else "",
            ])

        for col in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 34)

    wb.save(filepath)
    return filepath


def _build_pdf_sync(snapshots: list[dict], filepath: Path) -> Path:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_font("DejaVu", "", FONT_PATH)
    pdf.add_font("DejaVu", "B", FONT_BOLD_PATH)

    for snap in snapshots:
        pdf.add_page()
        pdf.set_font("DejaVu", "B", 14)
        pdf.multi_cell(0, 8, f"Отчёт — {snap['title']}", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("DejaVu", "", 10)
        pdf.multi_cell(
            0, 6,
            f"Договор: {snap['agreement_no']} | Период: {snap['agreement_start']} — {snap['agreement_finish']}",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(3)

        billing = snap["billing"]
        status_note = "" if billing.is_complete else " — предварительный расчёт"
        pdf.set_font("DejaVu", "B", 11)
        pdf.multi_cell(0, 7, f"Текущий расчёт ({billing.period_label}{status_note})", new_x="LMARGIN", new_y="NEXT")

        headers = ["Счётчик", "Пред.", "Тек.", "Расход", "Тариф", "Сумма (₽)"]
        widths = [32, 26, 26, 28, 36, 36]
        pdf.set_font("DejaVu", "B", 9)
        for h_txt, w in zip(headers, widths):
            pdf.cell(w, 7, h_txt, border=1, align="C")
        pdf.ln()

        is_irr_after_cold = (
            billing.irrigation_mode == IRRIGATION_MODE_AFTER_COLD
            and any(m in billing.meters for m in IRRIGATION_METERS)
        )
        pdf.set_font("DejaVu", "", 9)
        for mt in ALL_METERS:
            m = billing.meters.get(mt)
            if not m:
                continue
            if mt in IRRIGATION_METERS and is_irr_after_cold:
                tariff_str = "= ХВС"
                cost_str = "в ХВС"
            else:
                tariff_str = f"{float(m.tariff):.2f}" if m.tariff is not None else "—"
                cost_str = str(m.cost) if (m.has_base and m.has_tariff and m.submitted) else "—"
            row = [
                f"{METER_PLAIN_LABELS[mt]} ({METER_UNITS[mt]})",
                _fmt_opt(m.previous),
                _fmt_opt(m.current) if m.submitted else "—",
                str(m.consumption),
                tariff_str,
                cost_str,
            ]
            for val, w in zip(row, widths):
                pdf.cell(w, 7, val, border=1, align="C")
            pdf.ln()

        if billing.sewerage:
            sew = billing.sewerage
            sew_row = [
                "Водоотвед. (м³)",
                "—",
                "—",
                str(sew.consumption),
                f"{float(sew.tariff):.2f}" if sew.tariff is not None else "—",
                str(sew.cost) if (sew.has_base and sew.has_tariff and sew.submitted) else "—",
            ]
            for val, w in zip(sew_row, widths):
                pdf.cell(w, 7, val, border=1, align="C")
            pdf.ln()

        totals_parts = []
        if any(m in billing.meters for m in WATER_METERS):
            totals_parts.append(f"Вода: {fmt_num(billing.water_total)} ₽")
        if billing.sewerage:
            totals_parts.append(f"Водоотведение: {fmt_num(billing.sewerage_total)} ₽")
        if any(m in billing.meters for m in IRRIGATION_METERS) and not is_irr_after_cold:
            totals_parts.append(f"Полив: {fmt_num(billing.irrigation_total)} ₽")
        if any(m in billing.meters for m in ELECTRICITY_METERS):
            totals_parts.append(f"Электричество: {fmt_num(billing.electricity_total)} ₽")
        if any(m in billing.meters for m in GAS_METERS):
            totals_parts.append(f"Газ: {fmt_num(billing.gas_total)} ₽")
        total_suffix = "" if billing.is_complete else " (предв.)"
        totals_parts.append(f"ИТОГО: {fmt_num(billing.grand_total)} ₽{total_suffix}")

        pdf.set_font("DejaVu", "B", 10)
        pdf.ln(2)
        pdf.multi_cell(0, 6, "  |  ".join(totals_parts), new_x="LMARGIN", new_y="NEXT")
        if not billing.is_complete:
            pdf.set_font("DejaVu", "", 8)
            pdf.multi_cell(0, 5, f"Причина неполноты: {', '.join(billing.incomplete_reasons)}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        readings = snap["readings"]
        total_count = snap["total_count"]
        hist_title = (
            f"Последние показания (показано {len(readings)} из {total_count})"
            if total_count > len(readings)
            else f"История показаний (всего {total_count})"
        )
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 8, hist_title, new_x="LMARGIN", new_y="NEXT")
        if not readings:
            pdf.set_font("DejaVu", "", 9)
            pdf.cell(0, 6, "Показаний пока нет.", new_x="LMARGIN", new_y="NEXT")
        else:
            r_headers = ["Дата", "Счётчик", "Значение", "TG ID"]
            r_widths = [34, 52, 44, 54]

            def _draw_history_header() -> None:
                pdf.set_font("DejaVu", "B", 9)
                for h_txt, w in zip(r_headers, r_widths):
                    pdf.cell(w, 7, h_txt, border=1, align="C")
                pdf.ln()
                pdf.set_font("DejaVu", "", 9)

            _draw_history_header()
            for r in readings:
                if pdf.get_y() + 7 > pdf.page_break_trigger:
                    pdf.add_page()
                    _draw_history_header()
                base_lbl = METER_PLAIN_LABELS.get(r["meter_type"], r["meter_type"])
                if r.get("is_replacement_init"):
                    base_lbl = f"{base_lbl} (замена)"
                row = [
                    r["date"],
                    base_lbl,
                    f"{r['value']} {METER_UNITS.get(r['meter_type'], '')}",
                    str(r["submitted_by"]),
                ]
                for val, w in zip(row, r_widths):
                    pdf.cell(w, 6, val, border=1, align="C")
                pdf.ln()

    pdf.output(str(filepath))
    return filepath


async def generate_excel_report(apartments: list) -> Path:
    snapshots = await _collect_export_snapshots(apartments, EXCEL_HISTORY_LIMIT)
    ts = datetime.datetime.now(BOT_TZ).strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"utility_report_{ts}_{uuid.uuid4().hex[:8]}.xlsx"
    return await asyncio.to_thread(_build_excel_sync, snapshots, filepath)


async def generate_pdf_report(apartments: list) -> Path:
    snapshots = await _collect_export_snapshots(apartments, PDF_HISTORY_LIMIT)
    ts = datetime.datetime.now(BOT_TZ).strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"utility_report_{ts}_{uuid.uuid4().hex[:8]}.pdf"
    return await asyncio.to_thread(_build_pdf_sync, snapshots, filepath)
