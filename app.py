import streamlit as st
import sqlite3, json, io, os, math
from datetime import date, datetime, timedelta
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image, KeepTogether
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.lib.units import mm
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

APP_DIR=os.path.dirname(os.path.abspath(__file__))
DB=os.path.join(APP_DIR,'viking_power.db')
NAVY=colors.HexColor('#17365D'); BLUE=colors.HexColor('#315E9B'); ORANGE=colors.HexColor('#F28C28'); PALE=colors.HexColor('#F4F7FB'); GREEN=colors.HexColor('#15966A'); GREY=colors.HexColor('#64748B')

# ---------------- Database ----------------
def db():
    con=sqlite3.connect(DB, check_same_thread=False); con.row_factory=sqlite3.Row
    con.executescript('''
    CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, address TEXT, city TEXT, state TEXT, gstin TEXT, lead_source TEXT, roof_type TEXT, phase TEXT, assigned_to TEXT, notes TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, customer_id INTEGER, stage TEXT, system_kw REAL, system_type TEXT, next_followup TEXT, notes TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, category TEXT, brand TEXT, model TEXT, specification TEXT, unit TEXT, sell_price REAL, gst REAL, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, quote_no TEXT UNIQUE, customer_id INTEGER, proposal_date TEXT, capacity_kw REAL, system_type TEXT, phase TEXT, roof_type TEXT, structure TEXT, inverter_type TEXT, panel_make TEXT, panel_wp REAL, panel_qty INTEGER, inverter_make TEXT, inverter_capacity TEXT, epc_price REAL, gst_rate REAL, discount REAL, status TEXT, annual_generation REAL, monthly_bill REAL, tariff REAL, self_consumption REAL, export_rate REAL, tariff_escalation REAL, consumption_escalation REAL, payback_manual REAL, generation_manual REAL, savings_manual REAL, subsidy REAL, intro TEXT, terms TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS proposal_items(id INTEGER PRIMARY KEY, proposal_id INTEGER, description TEXT, make TEXT, capacity TEXT, quantity TEXT);
    CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, customer_id INTEGER, proposal_id INTEGER, name TEXT, capacity_kw REAL, contract_value REAL, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, invoice_no TEXT UNIQUE, customer_id INTEGER, project_id INTEGER, proposal_id INTEGER, invoice_date TEXT, due_date TEXT, taxable REAL, gst_rate REAL, total REAL, status TEXT, notes TEXT);
    CREATE TABLE IF NOT EXISTS invoice_items(
        id INTEGER PRIMARY KEY, invoice_id INTEGER, description TEXT, hsn_sac TEXT,
        quantity REAL, unit TEXT, rate REAL, taxable REAL, gst_rate REAL,
        cgst REAL, sgst REAL, igst REAL, total REAL
    );
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, invoice_id INTEGER, payment_date TEXT, amount REAL, method TEXT, reference TEXT, notes TEXT);
    '''); con.commit(); return con
CON=db()

def q(sql,args=()): return CON.execute(sql,args).fetchall()
def x(sql,args=()): CON.execute(sql,args); CON.commit()
def money(v): return f'₹{float(v or 0):,.2f}'
def num(v): return float(v or 0)
def financial_year(d=None):
    d = d or date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start+1)[-2:]}"

def next_no(prefix, table, col):
    fy = financial_year()
    rows = q(f'SELECT {col} FROM {table} WHERE {col} LIKE ? ORDER BY id DESC LIMIT 1',(f'{prefix}/%/{fy}',))
    n = 12 if fy == '2026-27' else 1  # Existing supplied invoice is VPE/011/2026-27
    if rows:
        try: n = int(rows[0][0].split('/')[1]) + 1
        except Exception: pass
    return f'{prefix}/{n:03d}/{fy}'

def init_products():
    if not q('SELECT id FROM products LIMIT 1'):
        seeds=[('Solar PV Module','EVVO','TOPCon 600+Wp','600+Wp','Nos',0,0),('Grid-tied inverter','EVVO','3-phase string inverter','4 kW','Nos',0,0),('Module structure','EVOST','Mini rail / standard','As per design','Lot',0,0),('AC/DC protection','SWIFT Europa','ACDB / DCDB / SCB','As per design','Set',0,0),('DC cable','Polycab','Solar DC cable','As per design','Mtr',0,0),('AC cable','Polycab','AC cable','As per design','Mtr',0,0),('Earth cable','Polycab','Earthing cable','As per design','Mtr',0,0),('Installation','Viking Power','EPC installation','Turnkey','Lot',0,0)]
        for a,b,c,d,e,f,g in seeds:x('INSERT INTO products(category,brand,model,specification,unit,sell_price,gst) VALUES(?,?,?,?,?,?,?)',(a,b,c,d,e,f,g))
init_products()

# ---------------- Savings model ----------------
def calc_savings(capacity, monthly_bill, tariff, self_use, export_rate, annual_gen, tariff_esc, cons_esc, manual=False, payback=0, gen_override=0, saving_override=0):
    gen=gen_override if manual and gen_override>0 else annual_gen
    gen=gen if gen>0 else capacity*1450
    annual_consumption=(monthly_bill*12/max(tariff,0.01))
    used=min(gen,annual_consumption)*max(0,min(100,self_use))/100
    exported=max(0,gen-used)
    annual_saving=used*tariff+exported*export_rate
    if manual and saving_override>0: annual_saving=saving_override
    payback=payback if manual and payback>0 else 0
    # cash payback estimated against EPC value elsewhere; this function returns yearly units and savings
    years=[]; cumulative=0
    for yr in range(1,16):
        t=tariff*((1+tariff_esc/100)**(yr-1)); cons=annual_consumption*((1+cons_esc/100)**(yr-1)); g=gen*(1-0.004)**(yr-1)
        self_units=min(g,cons)*max(0,min(100,self_use))/100; exp=max(0,g-self_units)
        save=self_units*t+exp*export_rate
        bill=max(0,cons*t-save)
        years.append({'year':yr,'tariff':t,'consumption':cons,'bill':cons*t,'generation':g,'savings':save,'after':bill})
    return gen,annual_saving,years

# ---------------- PDF generator ----------------
def build_pdf(p, customer, items, bank, portfolio_paths=None):
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=A4,rightMargin=17*mm,leftMargin=17*mm,topMargin=18*mm,bottomMargin=18*mm)
    styles=getSampleStyleSheet(); styles.add(ParagraphStyle(name='VPTitle',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=25,leading=30,textColor=NAVY,spaceAfter=8)); styles.add(ParagraphStyle(name='VPHead',parent=styles['Heading1'],fontSize=17,leading=21,textColor=NAVY,spaceAfter=12)); styles.add(ParagraphStyle(name='VPSub',parent=styles['Heading2'],fontSize=11,leading=14,textColor=BLUE,spaceBefore=6,spaceAfter=6)); styles.add(ParagraphStyle(name='VPBody',parent=styles['BodyText'],fontSize=9.3,leading=14,textColor=colors.HexColor('#273449'))); styles.add(ParagraphStyle(name='SmallGrey',parent=styles['BodyText'],fontSize=7.5,leading=10,textColor=GREY)); styles.add(ParagraphStyle(name='CenterBig',parent=styles['BodyText'],alignment=TA_CENTER,fontSize=15,leading=19,textColor=NAVY))
    story=[]
    def para(t,style='VPBody'): return Paragraph(str(t).replace('&','&amp;'),styles[style])
    def heading(t): story.append(para(t,'VPHead'))
    def table(data,widths=None,header=True):
        t=Table(data,colWidths=widths,repeatRows=1 if header else 0,hAlign='LEFT')
        cmds=[('GRID',(0,0),(-1,-1),0.45,colors.HexColor('#CBD5E1')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]
        if header: cmds += [('BACKGROUND',(0,0),(-1,0),NAVY),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold')]
        t.setStyle(TableStyle(cmds)); return t
    def page(): story.append(PageBreak())
    # cover
    story += [Spacer(1,20*mm),para('VIKING POWER','VPTitle'),para('EMPOWER WITH SOLAR','SmallGrey'),Spacer(1,18*mm),para('SOLAR ROOFTOP PROPOSAL','VPHead'),para(f"{p['capacity_kw']:g} kW {p['system_type']} Solar Power System",'CenterBig'),Spacer(1,12*mm),table([['PROPOSAL DETAILS',''],['Quote No.',p['quote_no']],['Date',p['proposal_date']],['Prepared for',customer['name']],['Location',customer['city'] or customer['address']],['Prepared by','Viking Power']], [55*mm,110*mm]),Spacer(1,30*mm),para('A carefully engineered solar solution for dependable generation, lower electricity costs and long-term value.','CenterBig'),Spacer(1,20*mm),para('Viking Power  |  info@vikingpower.in  |  +91-8722393939','SmallGrey'),PageBreak()]
    # welcome
    heading('WELCOME'); story += [para(f"Dear {customer['name']},"),Spacer(1,5*mm),para(p.get('intro') or 'Thank you for considering Viking Power for your solar project. We have prepared this proposal based on the information shared and the proposed site configuration. Our focus is on quality components, safe engineering, professional installation and dependable after-sales support.'),Spacer(1,8*mm),para('Our team will coordinate design finalisation, material procurement, installation, testing and commissioning. The final configuration will be confirmed after site verification and applicable utility requirements.'),Spacer(1,12*mm),para('Thanks &amp; Regards,'),para('Viking Power')]; page()
    # BOM
    heading('BILL OF MATERIALS')
    bom=[['Sl. No.','Description','Make','Capacity / Specification','Quantity']]
    for i,it in enumerate(items,1): bom.append([str(i),it['description'],it['make'],it['capacity'],it['quantity']])
    story += [table(bom,[13*mm,48*mm,30*mm,43*mm,25*mm]),Spacer(1,6*mm),para('Final quantities and specifications are subject to detailed engineering and site conditions.','SmallGrey')]; page()
    # technical + timeline
    heading('TECHNICAL PROPOSAL')
    tech=[['Parameter','Proposed configuration'],['Plant capacity',f"{p['capacity_kw']:g} kW"],['System type',p['system_type']],['Phase',p['phase']],['Structure',p['structure']],['Inverter type',p['inverter_type']],['Solar module',f"{p['panel_make']} – {p['panel_wp']:g} Wp × {p['panel_qty']} Nos."],['Inverter',f"{p['inverter_make']} – {p['inverter_capacity']}"]]
    story += [table(tech,[55*mm,105*mm]),Spacer(1,12*mm),para('PROJECT TIMELINE','VPSub'),table([['Stage','Indicative schedule'],['Design finalisation','Days 1–7'],['Engineering, procurement and supply','Days 7–21'],['Installation','Days 22–28'],['Testing and commissioning','Days 29–35']],[85*mm,75*mm]),para('Timeline is indicative and may change based on approvals, material availability, weather and site readiness.','SmallGrey')]; page()
    # commercial
    heading('COMMERCIAL OFFER')
    taxable=max(0,num(p['epc_price'])-num(p['discount'])); gst=taxable*num(p['gst_rate'])/100; total=taxable+gst
    story += [table([['Description','Amount'],[f"Turnkey EPC price for design, engineering, supply, installation, testing and commissioning of {p['capacity_kw']:g} kW solar plant",money(p['epc_price'])],['Discount',money(p['discount'])],[f"GST @ {p['gst_rate']:g}%",money(gst)],['TOTAL PROJECT COST',money(total)]],[112*mm,48*mm]),Spacer(1,5*mm),para(f"Rupees {int(total):,} only.",'VPSub'),Spacer(1,10*mm),para('BANK DETAILS','VPSub'),table([['Account name',bank.get('name','M/s. Viking Power')],['Account number',bank.get('account','To be configured')],['IFSC',bank.get('ifsc','To be configured')],['Bank',bank.get('bank','To be configured')],['Branch',bank.get('branch','Bengaluru')]], [50*mm,110*mm]),para('Verify bank details before sharing this proposal with a customer.','SmallGrey')]; page()
    # savings
    heading('SOLAR SAVINGS')
    gen,annual,years=calc_savings(p['capacity_kw'],p['monthly_bill'],p['tariff'],p['self_consumption'],p['export_rate'],p['annual_generation'],p['tariff_escalation'],p['consumption_escalation'],p['savings_mode']=='Manual override',p['payback_manual'],p['generation_manual'],p['savings_manual'])
    payback=num(p['epc_price'])/annual if annual>0 else 0
    kpis=[['Annual generation',f'{gen:,.0f} units'],['Estimated annual savings',money(annual)],['Estimated payback',f'{payback:.1f} years'],['Subsidy (if applicable)',money(p['subsidy'])]]
    story += [table([['INDICATOR','ESTIMATE'],*kpis],[80*mm,80*mm]),Spacer(1,7*mm),para('Environmental indicators are indicative estimates and depend on actual generation and the methodology selected. No fixed tree or CO₂ claim is made unless configured and verified.','SmallGrey'),Spacer(1,5*mm)]
    d=Drawing(165*mm,75*mm); chart=VerticalBarChart(); chart.x=18; chart.y=15; chart.height=55*mm; chart.width=145*mm; chart.data=[[years[i]['bill'] for i in range(0,15)],[years[i]['after'] for i in range(0,15)]]; chart.categoryAxis.categoryNames=[str(i) for i in range(1,16)]; chart.categoryAxis.labels.fontSize=6; chart.valueAxis.labels.fontSize=6; chart.valueAxis.valueMin=0; chart.valueAxis.valueMax=max(10000,max(y['bill'] for y in years)*1.1); chart.valueAxis.valueStep=max(1000,round(chart.valueAxis.valueMax/5,-3)); chart.bars[0].fillColor=BLUE; chart.bars[1].fillColor=GREEN; chart.groupSpacing=5; chart.barSpacing=1; d.add(chart); story += [d,para('Illustrative comparison of annual electricity bill before and after solar. Actual bills depend on consumption, tariff, export settlement and utility rules.','SmallGrey')]; page()
    # 15 years table
    heading('KNOW YOUR 15-YEAR ELECTRICITY BILLS')
    data=[['Year','Tariff','Consumption','Bill w/o solar','Generation','Savings','Bill after solar']]
    for z in years: data.append([str(z['year']),f"₹{z['tariff']:.2f}",f"{z['consumption']:,.0f}",f"{z['bill']:,.0f}",f"{z['generation']:,.0f}",f"{z['savings']:,.0f}",f"{z['after']:,.0f}"])
    story += [table(data,[12*mm,19*mm,25*mm,26*mm,23*mm,22*mm,28*mm]),Spacer(1,5*mm),para(f"Assumptions: tariff escalation {p['tariff_escalation']}% per year; consumption escalation {p['consumption_escalation']}% per year; module degradation modelled at 0.4% per year. These are editable modelling assumptions, not a guarantee.",'SmallGrey')]; page()
    # terms
    heading('GENERAL TERMS AND CONDITIONS')
    terms=p.get('terms') or 'Offer validity: 7 days from proposal date.\nPayment: 50% advance, 40% on material delivery and 10% after commissioning.\nWarranties: As per respective manufacturer warranty documents and agreed scope.\nForce majeure: Delivery and performance timelines may be affected by events beyond reasonable control.\nOrder confirmation: Work commences after written order confirmation and receipt of agreed advance.'
    for line in terms.splitlines():
        if line.strip(): story.append(para(line.strip())) ; story.append(Spacer(1,3*mm))
    page()
    # portfolio
    heading('PREVIOUS PROJECT HIGHLIGHTS')
    if portfolio_paths:
        imgs=[]
        for path in portfolio_paths[:4]:
            try: imgs.append(Image(path,width=75*mm,height=58*mm,kind='proportional'))
            except: pass
        if imgs: story.append(Table([[imgs[i] if i<len(imgs) else '' for i in range(0,2)],[imgs[i] if i<len(imgs) else '' for i in range(2,4)]],colWidths=[80*mm,80*mm],rowHeights=[65*mm,65*mm]))
    else: story += [Spacer(1,20*mm),para('Upload project photographs in Proposal Studio to populate this page.','CenterBig')]
    page()
    # contact
    story += [Spacer(1,20*mm),para('VIKING POWER','VPTitle'),para('Thank You!','CenterBig'),Spacer(1,15*mm),table([['CONTACT','DETAILS'],['Address','Ground Floor, No. 408, 4th G Main, HRBR Layout, Kalyan Nagar, Bengaluru – 560043'],['Phone','+91-8722393939'],['Email','info@vikingpower.in'],['Website','To be configured']],[35*mm,125*mm]),Spacer(1,20*mm),para('Empower with Solar','CenterBig')]
    def footer(canvas,doc):
        canvas.saveState(); w,h=A4; canvas.setStrokeColor(ORANGE); canvas.setLineWidth(2); canvas.line(17*mm,13*mm,w-17*mm,13*mm); canvas.setFont('Helvetica',8); canvas.setFillColor(GREY); canvas.drawString(17*mm,8*mm,'VIKING POWER  |  SOLAR ROOFTOP PROPOSAL'); canvas.drawRightString(w-17*mm,8*mm,f'{doc.page}'); canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer); return buf.getvalue()


# ---------------- GST invoice PDF ----------------
def inr_words(number):
    """Indian number-to-words helper for invoice totals."""
    n = int(round(float(number or 0)))
    if n == 0: return "ZERO RUPEES ONLY"
    ones = ["","ONE","TWO","THREE","FOUR","FIVE","SIX","SEVEN","EIGHT","NINE",
            "TEN","ELEVEN","TWELVE","THIRTEEN","FOURTEEN","FIFTEEN","SIXTEEN",
            "SEVENTEEN","EIGHTEEN","NINETEEN"]
    tens = ["","","TWENTY","THIRTY","FORTY","FIFTY","SIXTY","SEVENTY","EIGHTY","NINETY"]
    def under1000(x):
        out=[]
        if x>=100: out += [ones[x//100],"HUNDRED"]; x%=100
        if x>=20: out.append(tens[x//10]); x%=10
        if x: out.append(ones[x])
        return " ".join(out)
    parts=[]
    for value,label in [(n//10000000,"CRORE"),((n//100000)%100,"LAKH"),
                        ((n//1000)%100,"THOUSAND"),((n//100)%10,"HUNDRED"),(n%100,"")]:
        if value:
            if label in ("CRORE","LAKH","THOUSAND"):
                parts.append(under1000(value)+" "+label)
            else: parts.append(under1000(value))
    return " ".join(parts)+" RUPEES ONLY"

def build_invoice_pdf(inv, customer, items, bank, seller, payments):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=13*mm, leftMargin=13*mm,
                            topMargin=12*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="InvTitle", parent=styles["Title"], fontSize=20,
                              leading=24, textColor=NAVY, alignment=TA_LEFT, spaceAfter=4))
    styles.add(ParagraphStyle(name="InvSmall", parent=styles["BodyText"], fontSize=7.5,
                              leading=10, textColor=GREY))
    styles.add(ParagraphStyle(name="InvBody", parent=styles["BodyText"], fontSize=8,
                              leading=11, textColor=colors.HexColor("#1F2937")))
    styles.add(ParagraphStyle(name="InvHead", parent=styles["Heading2"], fontSize=9,
                              leading=12, textColor=NAVY, spaceAfter=4))
    P=lambda t,st="InvBody": Paragraph(str(t).replace("&","&amp;").replace("\\n","<br/>"),styles[st])
    story=[P("VIKING POWER","InvTitle"),P("Empower with Solar  |  TAX INVOICE","InvSmall"),Spacer(1,5*mm)]
    details=[
        [P("<b>Invoice No.</b>"),P(inv["invoice_no"]),P("<b>Invoice Date</b>"),P(inv["invoice_date"])],
        [P("<b>Reference No.</b>"),P(inv.get("reference_no") or "—"),P("<b>Due Date</b>"),P(inv.get("due_date") or "—")]
    ]
    t=Table(details,colWidths=[25*mm,65*mm,25*mm,65*mm])
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),PALE),("BOX",(0,0),(-1,-1),.5,colors.HexColor("#CBD5E1")),
                           ("INNERGRID",(0,0),(-1,-1),.35,colors.HexColor("#CBD5E1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                           ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story += [t,Spacer(1,5*mm)]
    seller_text=f"<b>{seller.get('name','M/s. Viking Power')}</b><br/>{seller.get('address','')}<br/>GSTIN: {seller.get('gstin','')}<br/>PAN: {seller.get('pan','')}<br/>Email: {seller.get('email','')}<br/>Phone: {seller.get('phone','')}"
    buyer_text=f"<b>{customer.get('name','')}</b><br/>{customer.get('address','')}<br/>{customer.get('city','')}, {customer.get('state','')}<br/>GSTIN: {customer.get('gstin') or 'Unregistered'}<br/>Phone: {customer.get('phone','')}<br/>Email: {customer.get('email','')}"
    parties=Table([[P("<b>BILLED BY</b><br/>"+seller_text),P("<b>BILLED TO</b><br/>"+buyer_text)]],colWidths=[90*mm,90*mm])
    parties.setStyle(TableStyle([("BOX",(0,0),(-1,-1),.5,colors.HexColor("#CBD5E1")),("INNERGRID",(0,0),(-1,-1),.5,colors.HexColor("#CBD5E1")),
                                ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
                                ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8)]))
    story += [parties,Spacer(1,5*mm)]
    data=[[P("<b>#</b>"),P("<b>Description / HSN-SAC</b>"),P("<b>Rate</b>"),P("<b>Qty</b>"),
            P("<b>Taxable</b>"),P("<b>GST</b>"),P("<b>CGST</b>"),P("<b>SGST</b>"),P("<b>Total</b>")]]
    for i,it in enumerate(items,1):
        desc=P(f"{it['description']}<br/><font size='7' color='#64748B'>HSN/SAC: {it['hsn_sac']}</font>")
        data.append([str(i),desc,f"₹{it['rate']:,.2f}",f"{it['quantity']:g}",f"₹{it['taxable']:,.2f}",
                     f"{it['gst_rate']:g}%",f"₹{it['cgst']:,.2f}",f"₹{it['sgst']:,.2f}",f"₹{it['total']:,.2f}"])
    widths=[8*mm,53*mm,20*mm,12*mm,23*mm,13*mm,19*mm,19*mm,23*mm]
    tbl=Table(data,colWidths=widths,repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                             ("GRID",(0,0),(-1,-1),.4,colors.HexColor("#CBD5E1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                             ("ALIGN",(2,1),(-1,-1),"RIGHT"),("FONTSIZE",(0,0),(-1,-1),7),
                             ("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),
                             ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [tbl,Spacer(1,4*mm)]
    taxable=sum(i["taxable"] for i in items); cgst=sum(i["cgst"] for i in items)
    sgst=sum(i["sgst"] for i in items); igst=sum(i["igst"] for i in items)
    total=sum(i["total"] for i in items); roundoff=round(total)-total; grand=round(total)
    taxrows=[[P("<b>Taxable value</b>"),P(f"₹{taxable:,.2f}"),P("<b>CGST</b>"),P(f"₹{cgst:,.2f}")],
             [P("<b>SGST</b>"),P(f"₹{sgst:,.2f}"),P("<b>IGST</b>"),P(f"₹{igst:,.2f}")],
             [P("<b>Round off</b>"),P(f"₹{roundoff:,.2f}"),P("<b>Total (INR)</b>"),P(f"<b>₹{grand:,.2f}</b>")]]
    tt=Table(taxrows,colWidths=[35*mm,50*mm,35*mm,50*mm])
    tt.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),PALE),("GRID",(0,0),(-1,-1),.4,colors.HexColor("#CBD5E1")),
                            ("ALIGN",(1,0),(1,-1),"RIGHT"),("ALIGN",(3,0),(3,-1),"RIGHT"),
                            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [tt,Spacer(1,3*mm),P(f"<b>Total (in words):</b> {inr_words(grand)}"),Spacer(1,4*mm)]
    story += [P("<b>Bank Details</b>","InvHead")]
    bankrows=[[P("Account Name"),P(bank.get("name","M/s Viking Power"))],
              [P("Account Number"),P(bank.get("account","258722393939"))],
              [P("IFSC"),P(bank.get("ifsc","INDB0000294"))],
              [P("Account Type"),P(bank.get("type","Current"))],
              [P("Bank"),P(bank.get("bank","IndusInd Bank"))]]
    bt=Table(bankrows,colWidths=[45*mm,125*mm])
    bt.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.4,colors.HexColor("#CBD5E1")),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    story += [bt,Spacer(1,3*mm)]
    if payments:
        received=sum(float(p["amount"]) for p in payments)
        story += [P(f"<b>Payment received:</b> ₹{received:,.2f} &nbsp;&nbsp; <b>Balance due:</b> ₹{grand-received:,.2f}"),Spacer(1,2*mm)]
    story += [P("Declaration: This is a computer-generated tax invoice. Please verify the particulars and applicable tax treatment before issue.","InvSmall")]
    def footer(canvas,doc):
        canvas.saveState(); w,h=A4; canvas.setStrokeColor(ORANGE); canvas.setLineWidth(1.5)
        canvas.line(13*mm,10*mm,w-13*mm,10*mm); canvas.setFont("Helvetica",7); canvas.setFillColor(GREY)
        canvas.drawString(13*mm,6*mm,"VIKING POWER  |  Empower with Solar")
        canvas.drawRightString(w-13*mm,6*mm,f"Page {doc.page}"); canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return buf.getvalue()


# ---------------- UI ----------------
st.set_page_config(page_title='Viking Power Studio',page_icon='☀️',layout='wide')
st.markdown('''<style>
:root{--vp-navy:#17365D;--vp-orange:#F28C28} .block-container{padding-top:1.5rem;max-width:1500px} h1,h2,h3{color:#17365D} div[data-testid="stMetric"]{background:#f4f7fb;padding:14px;border-radius:12px;border:1px solid #e2e8f0} .stButton>button[kind="primary"]{background:#17365D;border-color:#17365D}
</style>''',unsafe_allow_html=True)
st.title('☀️ Viking Power Studio')
st.caption('Solar CRM • Proposal Studio • Projects • Invoicing • Payments')
with st.sidebar:
    st.markdown('## VIKING POWER')
    menu=st.radio('WORKSPACE',['Dashboard','CRM','Products','Proposal Studio','Projects','Invoices & Payments','Settings'])
    st.caption('MVP • Local database • PDF proposal export')

if menu=='Dashboard':
    leads=q('SELECT COUNT(*) n FROM leads')[0]['n']; customers=q('SELECT COUNT(*) n FROM customers')[0]['n']; proposals=q('SELECT COUNT(*) n FROM proposals')[0]['n']; inv=q('SELECT COALESCE(SUM(total),0) n FROM invoices')[0]['n']; paid=q('SELECT COALESCE(SUM(amount),0) n FROM payments')[0]['n']
    st.subheader('Business overview')
    c=st.columns(5); [c[i].metric(label,val) for i,(label,val) in enumerate([('Customers',customers),('Leads',leads),('Proposals',proposals),('Invoiced',money(inv)),('Payments received',money(paid))])]
    st.subheader('Recent proposals')
    rows=q('SELECT p.quote_no,c.name,p.capacity_kw,p.system_type,p.epc_price,p.status,p.proposal_date FROM proposals p JOIN customers c ON c.id=p.customer_id ORDER BY p.id DESC LIMIT 10')
    st.dataframe([dict(r) for r in rows],use_container_width=True,hide_index=True)
    outstanding=inv-paid; st.metric('Indicative outstanding (all invoices)',money(outstanding))

elif menu=='CRM':
    st.subheader('CRM – Customers and leads')
    with st.expander('＋ Add customer / enquiry',expanded=True):
        with st.form('customer_form'):
            a,b,c=st.columns(3); name=a.text_input('Customer name*'); phone=b.text_input('Mobile*'); email=c.text_input('Email')
            address=st.text_input('Site address'); a,b,c=st.columns(3); city=a.text_input('City',value='Bengaluru'); state=b.text_input('State',value='Karnataka'); gstin=c.text_input('GSTIN')
            a,b,c=st.columns(3); source=a.selectbox('Lead source',['BNI','Referral','Website','Google','Social media','Walk-in','Other']); roof=b.selectbox('Roof type',['RCC','Sheet roof','Tile roof','Ground mount','Other']); phase=c.selectbox('Phase',['Single phase','Three phase'])
            a,b=st.columns(2); assigned=a.text_input('Assigned to'); kw=b.number_input('Estimated system kW',min_value=0.0,step=0.5); stage=st.selectbox('Pipeline stage',['New Enquiry','Contacted','Survey Booked','Survey Done','Quote Sent','Negotiation','Won','Lost']); follow=st.date_input('Next follow-up',value=date.today()+timedelta(days=1)); notes=st.text_area('Notes')
            if st.form_submit_button('Save customer and lead',type='primary'):
                if name and phone:
                    x('INSERT INTO customers(name,phone,email,address,city,state,gstin,lead_source,roof_type,phase,assigned_to,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(name,phone,email,address,city,state,gstin,source,roof,phase,assigned,notes,datetime.now().isoformat())); cid=CON.execute('SELECT last_insert_rowid()').fetchone()[0]
                    x('INSERT INTO leads(customer_id,stage,system_kw,system_type,next_followup,notes,created_at) VALUES(?,?,?,?,?,?,?)',(cid,stage,kw,'On-grid',str(follow),notes,datetime.now().isoformat())); st.success('Customer and lead saved.')
                else: st.error('Customer name and mobile are required.')
    st.dataframe([dict(r) for r in q('SELECT c.id,c.name,c.phone,c.city,c.roof_type,l.stage,l.system_kw,l.next_followup FROM customers c LEFT JOIN leads l ON l.customer_id=c.id ORDER BY c.id DESC')],use_container_width=True,hide_index=True)

elif menu=='Products':
    st.subheader('Product catalogue')
    with st.form('product_form'):
        a,b,c=st.columns(3); cat=a.selectbox('Category',['Solar PV Module','Grid-tied inverter','Hybrid inverter','Battery','Structure','ACDB','DCDB','Cable','Earthing','Installation','Other']); brand=b.text_input('Brand'); model=c.text_input('Model')
        a,b,c=st.columns(3); spec=a.text_input('Capacity / specification'); unit=b.selectbox('Unit',['Nos','Set','Lot','Mtr','Wp','kWh']); price=c.number_input('Internal selling price / unit (₹)',min_value=0.0,step=100.0)
        gst=st.number_input('GST %',min_value=0.0,max_value=100.0,value=0.0,step=0.1)
        if st.form_submit_button('Add product'):
            x('INSERT INTO products(category,brand,model,specification,unit,sell_price,gst) VALUES(?,?,?,?,?,?,?)',(cat,brand,model,spec,unit,price,gst)); st.success('Product added.')
    st.dataframe([dict(r) for r in q('SELECT id,category,brand,model,specification,unit,sell_price,gst FROM products ORDER BY category')],use_container_width=True,hide_index=True)

elif menu=='Proposal Studio':
    st.subheader('Proposal Studio')
    customers=q('SELECT id,name,city FROM customers ORDER BY name')
    if not customers: st.info('Add a customer in CRM first.'); st.stop()
    customer_map={f"{r['name']} – {r['city'] or ''} (#{r['id']})":r['id'] for r in customers}
    with st.form('proposal_form'):
        st.markdown('### 1. Customer and system')
        selected=st.selectbox('Customer',list(customer_map)); cid=customer_map[selected]
        a,b,c=st.columns(3); capacity=a.number_input('Plant capacity (kW)',min_value=0.5,value=3.0,step=0.5); system=b.selectbox('System type',['On-grid','Hybrid','Off-grid']); phase=c.selectbox('Phase',['Single phase','Three phase'])
        a,b,c=st.columns(3); roof=a.selectbox('Roof type',['RCC','Sheet roof','Tile roof','Ground mount']); structure=b.selectbox('Structure type',['Standard structure','Mini rail structure','Elevated structure','Carport']); inverter_type=c.selectbox('Inverter type',['String inverter','Hybrid inverter','Off-grid inverter','Microinverter'])
        st.markdown('### 2. Equipment configuration')
        a,b,c=st.columns(3); panel_make=a.text_input('Panel make',value='EVVO'); panel_wp=b.number_input('Panel watt-peak',min_value=100.0,value=600.0,step=5.0); panel_qty=c.number_input('Panel quantity',min_value=1,value=5,step=1)
        a,b=st.columns(2); inverter_make=a.text_input('Inverter make',value='EVVO'); inverter_capacity=b.text_input('Inverter capacity',value='4 kW, 3-phase')
        st.markdown('### 3. Bill of materials')
        catalog=q('SELECT category,brand,model,specification,unit FROM products WHERE active=1 ORDER BY category')
        catalog_bom='\n'.join([f"{r['category']} – {r['model']} | {r['brand']} | {r['specification']} | As per design" for r in catalog])
        default_bom=catalog_bom or 'Solar PV Module | EVVO | 600+Wp | 05 Nos.\nSolar grid-tied inverter | EVVO | 4kW 3-phase | 1 No.\nModule structure | EVOST | As per design | As per design\nAC/DC combiner boxes | SWIFT Europa | As per design | As per design\nDC cables | Polycab / Siechem | As per design | As per design'
        st.caption('The starter BOM is prefilled from your Product Catalogue. Edit descriptions, makes, capacities and quantities for this proposal.')
        bom_text=st.text_area('One item per line: Description | Make | Capacity | Quantity',value=default_bom,height=180)
        st.markdown('### 4. Commercial offer – EPC price')
        a,b,c=st.columns(3); epc=a.number_input('Consolidated EPC price (before GST) ₹',min_value=0.0,value=291000.0,step=5000.0); discount=b.number_input('Discount ₹',min_value=0.0,step=1000.0); gst=c.number_input('GST %',min_value=0.0,value=8.9,step=0.1)
        st.markdown('### 5. Savings assumptions')
        a,b,c=st.columns(3); bill=a.number_input('Current monthly bill ₹',min_value=0.0,value=2500.0,step=100.0); tariff=b.number_input('Effective tariff ₹/unit',min_value=0.1,value=6.5,step=0.1); annual_gen=c.number_input('Annual generation units (0 = estimate)',min_value=0.0,value=0.0,step=100.0)
        a,b,c=st.columns(3); selfuse=a.slider('Self-consumption %',0,100,80); export=b.number_input('Export value ₹/unit',min_value=0.0,value=0.0,step=0.1); subsidy=c.number_input('Subsidy shown ₹ (if applicable)',min_value=0.0,step=1000.0)
        a,b,c=st.columns(3); tariff_esc=a.number_input('Annual tariff escalation %',min_value=0.0,value=2.0,step=0.5); cons_esc=b.number_input('Annual consumption escalation %',min_value=0.0,value=1.0,step=0.5); mode=c.selectbox('Savings mode',['Automatic','Manual override'])
        a,b,c=st.columns(3); gen_manual=a.number_input('Manual annual generation units',min_value=0.0,value=0.0); saving_manual=b.number_input('Manual annual savings ₹',min_value=0.0,value=0.0); payback_manual=c.number_input('Manual payback years (reference)',min_value=0.0,value=0.0)
        intro=st.text_area('Welcome letter text',value='Thank you for considering Viking Power for your solar project. We are committed to quality components, sound engineering and professional installation.'); terms=st.text_area('Terms and conditions',value='Offer validity: 7 days from proposal date.\nPayment: 50% advance, 40% upon material delivery and 10% after commissioning.\nWarranties: As per manufacturer warranty documents.\nForce majeure: Delays due to events beyond reasonable control are excluded.\nOrder confirmation: Written order confirmation and agreed advance are required.')
        if st.form_submit_button('Save proposal and generate PDF',type='primary'):
            quote=next_no('VPQ', 'proposals','quote_no'); today=str(date.today())
            x('INSERT INTO proposals(quote_no,customer_id,proposal_date,capacity_kw,system_type,phase,roof_type,structure,inverter_type,panel_make,panel_wp,panel_qty,inverter_make,inverter_capacity,epc_price,gst_rate,discount,status,annual_generation,monthly_bill,tariff,self_consumption,export_rate,tariff_escalation,consumption_escalation,payback_manual,generation_manual,savings_manual,subsidy,intro,terms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(quote,cid,today,capacity,system,phase,roof,structure,inverter_type,panel_make,panel_wp,int(panel_qty),inverter_make,inverter_capacity,epc,gst,discount,'Draft',annual_gen,bill,tariff,selfuse,export,tariff_esc,cons_esc,payback_manual,gen_manual,saving_manual,subsidy,intro,terms))
            pid=CON.execute('SELECT last_insert_rowid()').fetchone()[0]
            parsed=[]
            for line in bom_text.splitlines():
                parts=[z.strip() for z in line.split('|')]
                if len(parts)>=4:
                    item={'description':parts[0],'make':parts[1],'capacity':parts[2],'quantity':parts[3]}; parsed.append(item); x('INSERT INTO proposal_items(proposal_id,description,make,capacity,quantity) VALUES(?,?,?,?,?)',(pid,*[item[k] for k in ['description','make','capacity','quantity']]))
            p=dict(q('SELECT * FROM proposals WHERE id=?',(pid,))[0]); cust=dict(q('SELECT * FROM customers WHERE id=?',(cid,))[0]); bank={'name':'M/s. Viking Power','account':'Configure in Settings','ifsc':'Configure in Settings','bank':'Configure in Settings','branch':'Bengaluru'}
            pdf=build_pdf(p,cust,parsed,bank)
            st.session_state['latest_pdf']=pdf; st.session_state['latest_quote']=quote; st.session_state['latest_pid']=pid
            st.success(f'Proposal {quote} saved. PDF generated.')
    if st.session_state.get('latest_pdf'):
        st.download_button('⬇ Download customer proposal PDF',data=st.session_state['latest_pdf'],file_name=f"{st.session_state['latest_quote']}.pdf",mime='application/pdf',type='primary')
        st.caption('Proposal is saved in the database. Mark it Sent / Accepted in the proposal register below.')
    st.markdown('### Proposal register')
    props=q('SELECT p.id,p.quote_no,c.name,p.capacity_kw,p.system_type,p.epc_price,p.status,p.proposal_date FROM proposals p JOIN customers c ON c.id=p.customer_id ORDER BY p.id DESC')
    st.dataframe([dict(r) for r in props],use_container_width=True,hide_index=True)
    if props:
        with st.form('proposal_status'):
            pid=st.selectbox('Select proposal ID to update', [r['id'] for r in props]); status=st.selectbox('Status',['Draft','Sent','Accepted','Rejected','Expired'])
            if st.form_submit_button('Update proposal status'):
                x('UPDATE proposals SET status=? WHERE id=?',(status,pid)); st.success('Proposal status updated.')
                if status=='Accepted':
                    p=q('SELECT * FROM proposals WHERE id=?',(pid,))[0]; exists=q('SELECT id FROM projects WHERE proposal_id=?',(pid,))
                    if not exists:
                        x('INSERT INTO projects(customer_id,proposal_id,name,capacity_kw,contract_value,status,created_at) VALUES(?,?,?,?,?,?,?)',(p['customer_id'],pid,f"{p['capacity_kw']} kW {p['system_type']} Solar",p['capacity_kw'],(p['epc_price']-p['discount'])*(1+p['gst_rate']/100),'Won',datetime.now().isoformat())); st.info('Accepted proposal converted to a project. You can now invoice it in Projects / Invoices.')

elif menu=='Projects':
    st.subheader('Projects')
    rows=q('SELECT pr.id,pr.name,c.name customer,pr.capacity_kw,pr.contract_value,pr.status FROM projects pr JOIN customers c ON c.id=pr.customer_id ORDER BY pr.id DESC')
    st.dataframe([dict(r) for r in rows],use_container_width=True,hide_index=True)
    if rows:
        with st.form('project_status'):
            pid=st.selectbox('Project ID',[r['id'] for r in rows]); status=st.selectbox('Project status',['Won','Design','Procurement','Installation','Commissioning','Completed','On hold'])
            if st.form_submit_button('Update project'): x('UPDATE projects SET status=? WHERE id=?',(status,pid)); st.success('Project updated.')

elif menu=='Invoices & Payments':
    st.subheader('Invoices & Payments')
    st.caption('GST invoice generation • EPC or item-wise billing • financial-year numbering • payment reconciliation')
    projects=q('SELECT pr.id,pr.name,c.name customer,pr.customer_id,pr.proposal_id,pr.contract_value,p.epc_price,p.gst_rate,p.discount FROM projects pr JOIN customers c ON c.id=pr.customer_id LEFT JOIN proposals p ON p.id=pr.proposal_id ORDER BY pr.id DESC')
    if projects:
        with st.expander('＋ Create tax invoice',expanded=True):
            pm={f"{r['name']} – {r['customer']} (Project #{r['id']})":r for r in projects}
            with st.form('invoice_form_v2'):
                sel=st.selectbox('Linked project / accepted proposal',list(pm)); pr=pm[sel]
                a,b,c=st.columns(3)
                inv_date=a.date_input('Invoice date',value=date.today())
                due=b.date_input('Due date',value=date.today()+timedelta(days=7))
                billing_mode=c.selectbox('Billing format',['Consolidated EPC','Item-wise'])
                invoice_no=st.text_input('Invoice number (editable)',value=next_no('VPE','invoices','invoice_no'),
                                         help='Suggested financial-year sequence. You can edit this before saving; duplicate numbers are not allowed.')
                st.markdown('**Tax treatment**')
                a,b,c=st.columns(3)
                place_state=a.text_input('Place of supply / state',value='Karnataka')
                supply_type=b.selectbox('Supply type',['Intra-state (CGST + SGST)','Inter-state (IGST)'])
                reference=c.text_input('Reference / work order no.')
                if billing_mode=='Consolidated EPC':
                    taxable_default=max(0.0,float(pr['epc_price'] or 0)-float(pr['discount'] or 0))
                    a,b,c=st.columns(3)
                    desc=a.text_input('Invoice description',value=f"{pr['name']} – Turnkey EPC supply, installation, testing and commissioning")
                    hsn=a.text_input('HSN/SAC',value='995426')
                    rate=b.number_input('Taxable EPC value ₹',min_value=0.0,value=taxable_default,step=1000.0)
                    gst=c.number_input('GST %',min_value=0.0,max_value=100.0,value=float(pr['gst_rate'] or 18.0),step=0.1)
                    lines_text=None
                else:
                    st.caption('Enter one line per row using: Description | HSN/SAC | Quantity | Unit | Rate ₹ | GST %')
                    default_lines="Supply of solar PV modules | 85414300 | 1 | Lot | 0 | 5\nBalance of system supply | 85371000 | 1 | Lot | 0 | 18\nInstallation, testing and commissioning | 995426 | 1 | Job | 0 | 18"
                    lines_text=st.text_area('Invoice line items',value=default_lines,height=145)
                    desc=''; hsn=''; rate=0.0; gst=0.0
                notes=st.text_input('Notes / payment terms')
                if st.form_submit_button('Create invoice and save',type='primary'):
                    raw=[]
                    if billing_mode=='Consolidated EPC':
                        raw=[{'description':desc,'hsn_sac':hsn,'quantity':1.0,'unit':'Job','rate':rate,'gst_rate':gst}]
                    else:
                        try:
                            for line in lines_text.splitlines():
                                if not line.strip(): continue
                                parts=[z.strip() for z in line.split('|')]
                                if len(parts)!=6: raise ValueError('Each line must have six fields separated by |')
                                raw.append({'description':parts[0],'hsn_sac':parts[1],'quantity':float(parts[2]),'unit':parts[3],'rate':float(parts[4]),'gst_rate':float(parts[5])})
                        except Exception as e:
                            st.error(f'Could not read invoice items: {e}'); raw=[]
                    if raw:
                        prepared=[]; total_taxable=total=0.0
                        for it in raw:
                            taxable=it['quantity']*it['rate']; tax=taxable*it['gst_rate']/100
                            cgst=sgst=0.0; igst=0.0
                            if supply_type.startswith('Intra'):
                                cgst=tax/2; sgst=tax/2
                            else: igst=tax
                            row={**it,'taxable':taxable,'cgst':cgst,'sgst':sgst,'igst':igst,'total':taxable+tax}
                            prepared.append(row); total_taxable+=taxable; total+=row['total']
                        inv_no=invoice_no.strip()
                        duplicate=q('SELECT id FROM invoices WHERE invoice_no=?',(inv_no,))
                        if not inv_no:
                            st.error('Please enter an invoice number.')
                        elif duplicate:
                            st.error(f'Invoice number {inv_no} already exists. Please enter a unique number.')
                        else:
                            rounded=round(total)
                            status='Unpaid'
                            x('INSERT INTO invoices(invoice_no,customer_id,project_id,proposal_id,invoice_date,due_date,taxable,gst_rate,total,status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (inv_no,pr['customer_id'],pr['id'],pr['proposal_id'],str(inv_date),str(due),total_taxable,0.0,rounded,status,notes))
                            invoice_id=CON.execute('SELECT last_insert_rowid()').fetchone()[0]
                            for it in prepared:
                                x('INSERT INTO invoice_items(invoice_id,description,hsn_sac,quantity,unit,rate,taxable,gst_rate,cgst,sgst,igst,total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (invoice_id,it['description'],it['hsn_sac'],it['quantity'],it['unit'],it['rate'],it['taxable'],it['gst_rate'],it['cgst'],it['sgst'],it['igst'],it['total']))
                            st.success(f'Invoice {inv_no} created. Financial-year numbering: {financial_year(inv_date)}.')
                            st.rerun()
    else:
        st.info('Accept a proposal first. The accepted proposal will create a project available for invoicing.')

    invoices=q('SELECT i.*,c.name customer,COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id=i.id),0) received FROM invoices i JOIN customers c ON c.id=i.customer_id ORDER BY i.id DESC')
    st.markdown('### Record payment')
    if invoices:
        with st.form('payment_form_v2'):
            im={f"{r['invoice_no']} – {r['customer']} | Balance {money(max(0,r['total']-r['received']))}":r for r in invoices}
            sel=st.selectbox('Invoice',list(im)); inv=im[sel]
            a,b,c=st.columns(3)
            balance=max(0.0,float(inv['total']-inv['received']))
            amount=a.number_input('Payment amount ₹',min_value=0.0,max_value=max(balance,0.0),value=balance,step=100.0)
            method=b.selectbox('Method',['UPI','Bank transfer','Cash','Cheque','Card','Other'])
            pdate=c.date_input('Payment date',value=date.today())
            ref=st.text_input('Transaction reference'); note=st.text_input('Payment notes')
            if st.form_submit_button('Record payment'):
                if amount>0 and amount<=balance+0.01:
                    x('INSERT INTO payments(invoice_id,payment_date,amount,method,reference,notes) VALUES(?,?,?,?,?,?)',(inv['id'],str(pdate),amount,method,ref,note))
                    received=inv['received']+amount
                    status='Paid' if received>=inv['total']-0.01 else 'Partially paid'
                    x('UPDATE invoices SET status=? WHERE id=?',(status,inv['id']))
                    st.success('Payment recorded and balance updated.'); st.rerun()
                else: st.error('Payment must be greater than zero and cannot exceed the outstanding balance.')

    st.markdown('### Invoice register & PDF downloads')
    invoices=q('SELECT i.*,c.name customer,COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id=i.id),0) received FROM invoices i JOIN customers c ON c.id=i.customer_id ORDER BY i.id DESC')
    if invoices:
        st.dataframe([{'Invoice':r['invoice_no'],'Customer':r['customer'],'Date':r['invoice_date'],'Total':money(r['total']),'Received':money(r['received']),'Balance':money(max(0,r['total']-r['received'])),'Status':r['status']} for r in invoices],use_container_width=True,hide_index=True)
        inv_map={f"{r['invoice_no']} – {r['customer']}":r for r in invoices}
        chosen=st.selectbox('Select invoice to preview / download',list(inv_map))
        inv=dict(inv_map[chosen])
        customer=dict(q('SELECT * FROM customers WHERE id=?',(inv['customer_id'],))[0])
        item_rows=q('SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id',(inv['id'],))
        items=[dict(z) for z in item_rows]
        if not items:
            # Backward compatibility for invoices created by the first MVP.
            items=[{'description':inv.get('notes') or 'Solar EPC supply and installation','hsn_sac':'995426','quantity':1,'unit':'Job',
                    'rate':inv['taxable'],'taxable':inv['taxable'],'gst_rate':inv['gst_rate'],'cgst':inv['taxable']*inv['gst_rate']/200,
                    'sgst':inv['taxable']*inv['gst_rate']/200,'igst':0,'total':inv['total']}]
        payment_rows=[dict(z) for z in q('SELECT * FROM payments WHERE invoice_id=? ORDER BY payment_date',(inv['id'],))]
        bank={'name':'M/s Viking Power','account':'258722393939','ifsc':'INDB0000294','type':'Current','bank':'IndusInd Bank'}
        seller={'name':'M/s. Viking Power','address':'No. 408, Ground Floor, 4th G Main, H.R.B.R Layout, 2nd Block, Kalyan Nagar, Bengaluru, Karnataka - 560043',
                'gstin':'29AAYFV9039Q1ZL','pan':'AAYFV9039Q','email':'manjukiran@vikingpower.in','phone':'+91 87223 93939'}
        invoice_pdf=build_invoice_pdf(inv,customer,items,bank,seller,payment_rows)
        st.download_button('Download GST tax invoice PDF',data=invoice_pdf,file_name=f"{inv['invoice_no'].replace('/','-')}.pdf",mime='application/pdf',type='primary')
    else:
        st.info('No invoices created yet.')

elif menu=='Settings':
    st.subheader('Company, proposal and invoicing settings')
    st.info('This starter uses demo company details from the uploaded proposal. Before live use, verify bank account, IFSC, GSTIN, address, email, phone, warranty wording and applicable GST rates.')
    st.markdown('**Current sample contact details**')
    st.write('Viking Power | Ground Floor, No. 408, 4th G Main, HRBR Layout, Kalyan Nagar, Bengaluru – 560043 | +91-8722393939 | info@vikingpower.in')
    st.warning('This is an initial single-workspace MVP. Add authentication, role permissions, cloud PostgreSQL, automated backups and a verified GST invoice workflow before storing live customer financial data.')
