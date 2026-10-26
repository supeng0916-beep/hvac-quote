"""Neutral quotation workbook output; uploaded price books remain read-only."""
from __future__ import annotations
import subprocess
import shutil
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

UPPERCASE_SHEET = "_uppercase"
UPPERCASE_DIGITS = "零壹贰叁肆伍陆柒捌玖"

@dataclass(frozen=True)
class RecalcResult:
    workbook_path: Path
    values: dict[str, Decimal]
    texts: dict[str, str] = field(default_factory=dict)

def _group_digits_formula(ref: str) -> str:
    thousands = f"MOD(INT({ref}/1000),10)"
    hundreds = f"MOD(INT({ref}/100),10)"
    tens = f"MOD(INT({ref}/10),10)"
    ones = f"MOD({ref},10)"
    return (
        f'IF({thousands}=0,"零",MID($A$19,{thousands}+1,1)&"仟")'
        f'&IF({hundreds}=0,"零",MID($A$19,{hundreds}+1,1)&"佰")'
        f'&IF({tens}=0,"零",MID($A$19,{tens}+1,1)&"拾")'
        f'&IF({ones}=0,"零",MID($A$19,{ones}+1,1))'
    )


def _write_uppercase_sheet(workbook, total_ref: str) -> None:
    """用普通公式把最终含税总额转成人民币大写。

    隐藏表按“亿/万/个”三组拆分，组内先拼接再清理多余的“零”，
    与 money.upper_amount 的口径保持一致；不使用宏或宏表函数。
    """
    if UPPERCASE_SHEET in workbook.sheetnames:
        sheet = workbook[UPPERCASE_SHEET]
    else:
        sheet = workbook.create_sheet(UPPERCASE_SHEET)
    sheet.sheet_state = "hidden"
    sheet["A19"] = UPPERCASE_DIGITS
    sheet["A1"] = f"=ROUND({total_ref},2)"
    sheet["A2"] = "=INT(A1)"
    sheet["A3"] = "=ROUND(A1*100,0)-A2*100"
    sheet["A4"] = "=INT(A3/10)"
    sheet["A5"] = "=A3-A4*10"
    sheet["A7"] = "=INT(A2/100000000)"
    sheet["A8"] = "=INT(A2/10000)-A7*10000"
    sheet["A9"] = "=A2-INT(A2/10000)*10000"
    for row in (7, 8, 9):
        sheet[f"C{row}"] = f'=IF(A{row}=0,"",{_group_digits_formula(f"A{row}")})'
        sheet[f"D{row}"] = (
            f'=IF(C{row}="","",SUBSTITUTE(SUBSTITUTE(SUBSTITUTE('
            f'SUBSTITUTE(SUBSTITUTE(C{row},"零仟","零"),"零佰","零"),'
            f'"零拾","零"),"零零","零"),"零零","零"))'
        )
        sheet[f"E{row}"] = (
            f'=IF(D{row}="","",IF(LEFT(D{row},1)="零",'
            f'MID(D{row},2,99),D{row}))'
        )
        sheet[f"F{row}"] = (
            f'=IF(E{row}="","",IF(RIGHT(E{row},1)="零",'
            f'LEFT(E{row},LEN(E{row})-1),E{row}))'
        )
    sheet["A11"] = (
        '=IF(A2=0,"零",'
        'F7&IF(A7>0,"亿","")'
        '&IF(AND(A7>0,A8>0,A8<1000),"零","")'
        '&IF(A8>0,F8&"万","")'
        '&IF(A9>0,IF(AND(OR(A7>0,A8>0),A9<1000),"零",""),"")'
        '&IF(A9>0,F9,""))'
    )
    sheet["A13"] = '=IF(A4=0,"",MID($A$19,A4+1,1)&"角")'
    sheet["A14"] = '=IF(A5=0,"",MID($A$19,A5+1,1)&"分")'
    sheet["A15"] = '=IF(AND(A4=0,A5>0),"零","")'
    sheet["A17"] = '=A11&"元"&IF(A3=0,"整",A13&A15&A14)'


def _soffice() -> str:
    found = shutil.which("soffice")
    if found:
        return found
    fallback = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("未找到 LibreOffice soffice，无法重算/导出 PDF")


def _convert(source: Path, outdir: Path, target: str, profile: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    command = [
        _soffice(),
        "--headless",
        f"-env:UserInstallation=file://{profile.resolve()}",
        "--convert-to",
        target,
        "--outdir",
        str(outdir),
        str(source),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"LibreOffice 转换失败（{target}）："
            f"{completed.stderr[-1000:] or completed.stdout[-1000:]}"
        )



def export_workbook(path: Path, *, project_name: str, pricing, items, labels: dict, tax_rate: Decimal, scope: dict) -> Path:
    """Write confirmed lines to a fresh workbook, independent of source layout.

    No cells, formulas, metadata, macros or hidden sheets from the input are copied.
    Monetary formulas are recalculated by LibreOffice and checked against Python.
    """
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    workbook=Workbook();sheet=workbook.active;sheet.title='报价明细'
    workbook.properties.creator='HVAC Quote';workbook.properties.lastModifiedBy='HVAC Quote'
    def text(row,col,value):
        cell=sheet.cell(row,col,str(value or ''));cell.data_type='s'
    text(1,1,project_name)
    for row,label in [(3,'未税合计'),(4,'税额'),(5,'含税合计'),(6,'税率 / 加计比例'),(7,'计税基数')]:text(row,1,label)
    text(2,1,'候选经人工确认；未计入范围见“报价范围”。')
    headers=['序号','分类','名称','规格','单位','数量','材料单价','安装单价','材料金额','安装金额','材料系数','安装系数','未税合价']
    for col,title in enumerate(headers,1):
        text(9,col,title);cell=sheet.cell(9,col);cell.fill=PatternFill('solid',fgColor='18334A');cell.font=Font(color='FFFFFF',bold=True)
    item_map={item.item_id:item for item in items}
    tax_terms=[]
    for row,line in enumerate(pricing.lines,10):
        item=item_map[line.item_id];label=labels.get(line.template_row,{})
        for col,value in {1:row-9,2:'设备' if line.bucket=='equipment' else '管线 / 辅材',3:line.display_name or label.get('name') or item.spec,4:line.display_spec or label.get('spec') or item.spec,5:item.unit}.items():text(row,col,value)
        for col,value in {6:line.quantity,7:line.material_unit_price,8:line.installation_unit_price,11:pricing.coefficients[line.bucket+'_material'],12:pricing.coefficients[line.bucket+'_installation']}.items():sheet.cell(row,col,float(value))
        sheet.cell(row,9,f'=F{row}*G{row}');sheet.cell(row,10,f'=F{row}*H{row}')
        sheet.cell(row,13,f'=I{row}*K{row}+J{row}*L{row}')
        tax_terms.append(f'J{row}*L{row}' if pricing.tax_basis=='installation_and_auxiliary' and line.bucket=='equipment' else f'M{row}')
    last=9+len(pricing.lines)
    sheet['B3']=f'=SUM(M10:M{last})';sheet['B6']=float(tax_rate)
    sheet['B7']='='+'+'.join(tax_terms)
    sheet['B5']='=ROUND(B3+B6*B7,2)';sheet['B4']='=B5-ROUND(B3,2)'
    _write_uppercase_sheet(workbook,"'报价明细'!B5")
    sheet['D5']='含税合计（大写）';sheet['E5']='=_uppercase!A17'
    for row in sheet.iter_rows(min_row=10,max_row=last,min_col=6,max_col=13):
        for cell in row:cell.number_format='#,##0.###'
    for row in (3,4,5,7):sheet.cell(row,2).number_format='#,##0.00'
    sheet['B6'].number_format='0.##%'
    for col in range(1,14):sheet.column_dimensions[get_column_letter(col)].width=15
    sheet.column_dimensions['C'].width=28;sheet.column_dimensions['D'].width=24
    sheet.freeze_panes='F10';sheet.auto_filter.ref=f'A9:M{last}'
    sheet.sheet_properties.pageSetUpPr.fitToPage=True
    sheet.page_setup.orientation='landscape';sheet.page_setup.paperSize=sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth=1;sheet.page_setup.fitToHeight=0
    sheet.print_title_rows='1:9';sheet.print_area=f'A1:M{last}'
    details=workbook.create_sheet('报价范围')
    for key,value in scope.items():
        row=details.max_row+1
        for col,content in [(1,key),(2,'；'.join(value) if isinstance(value,list) else str(value or ''))]:
            cell=details.cell(row,col,content);cell.data_type='s';cell.alignment=Alignment(wrap_text=True)
    details.column_dimensions['A'].width=22;details.column_dimensions['B'].width=90
    workbook.save(path)
    return path


def recalc_workbook(workbook_path: Path, out_dir: Path) -> RecalcResult:
    workbook_path=Path(workbook_path);out_dir=Path(out_dir)
    recalc_dir=out_dir/'recalc'
    _convert(workbook_path,recalc_dir,'xlsx',out_dir/'lo-profile')
    recalculated=recalc_dir/workbook_path.name
    if not recalculated.is_file():raise RuntimeError('未生成重算后的报价表。')
    workbook=load_workbook(recalculated,data_only=True);sheet=workbook['报价明细']
    values={}
    for name,cell in {'subtotal':'B3','total':'B5'}.items():
        value=sheet[cell].value
        if not isinstance(value,(int,float)):raise RuntimeError(f'重算后 {cell} 没有有效金额。')
        values[name]=Decimal(str(value))
    return RecalcResult(recalculated,values,{'uppercase':str(sheet['E5'].value or '')})
