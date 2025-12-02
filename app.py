import os
import json
import time
import pandas as pd
import openpyxl
from openpyxl.cell.cell import MergedCell
import streamlit as st
import google.generativeai as genai
from dotenv import load_dotenv

# --- 設定 ---
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# ▼▼▼ モデル指定 ▼▼▼
MODEL_NAME = "gemini-2.5-pro"
TEMPLATE_FILE = "template.xlsx"

# ▼▼▼ 合言葉の設定 ▼▼▼
LOGIN_PASSWORD = "fujishima8888" 

# --- ページ設定 ---
st.set_page_config(page_title="経費精算AI (Ver.3.1 高速代対応)", layout="wide")

# ▼▼▼ CSSスタイル ▼▼▼
st.markdown("""
    <style>
    [data-testid="stFileUploaderDropzoneInstructions"] > div > span {display: none;}
    [data-testid="stFileUploaderDropzoneInstructions"] > div::after { content: "ファイルをドラッグまたは選択"; font-weight: bold; font-size: 1rem; }
    [data-testid="stFileUploaderDropzoneInstructions"] > div > small {display: none;}
    [data-testid="stFileUploaderDropzoneInstructions"] > div::before { content: "上限 200MB / PDFのみ"; font-size: 0.8rem; display: block; margin-bottom: 5px; }
    [data-testid="stMetric"] { background-color: #f0f2f6; padding: 15px; border-radius: 10px; border: 1px solid #e0e0e0; }
    @media (prefers-color-scheme: dark) { [data-testid="stMetric"] { background-color: #262730; border: 1px solid #41444e; } }
    </style>
""", unsafe_allow_html=True)

# --- 認証機能 ---
def check_password():
    if 'authenticated' not in st.session_state: st.session_state['authenticated'] = False
    if st.session_state['authenticated']: return True
    st.title("🔒 ログイン")
    password = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if password == LOGIN_PASSWORD:
            st.session_state['authenticated'] = True
            st.rerun()
        else: st.error("パスワードが違います")
    return False

# --- 結合セル対応書き込み ---
def smart_write(ws, row, col, value):
    cell = ws.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        for merged_range in ws.merged_cells.ranges:
            if cell.coordinate in merged_range:
                ws.cell(row=merged_range.min_row, column=merged_range.min_col).value = value
                return
    else: cell.value = value

# --- ▼▼▼ 集計・分類ロジック（高速代を追加） ▼▼▼ ---
def aggregate_receipt_data(raw_data):
    """
    データを「交通費」「駐車場」「高速代」「一般」の4つに分類して集計する
    """
    df = pd.DataFrame(raw_data)
    # データが空の場合の初期化
    if df.empty: 
        return {"transport": None, "parking": None, "highway": None, "general": []}

    # 数値変換
    cols_to_num = ['total_amount', 'amount_8_percent']
    for col in cols_to_num:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    result_dict = {
        "transport": None, # 9行目用 (電車・バス)
        "parking": None,   # 10行目用 (駐車場)
        "highway": None,   # 11行目以降の先頭 (高速代)
        "general": []      # 11行目以降 (その他)
    }

    # --- 1. 交通費 (transport: 電車・バス) ---
    df_trans = df[df['category'] == 'transport']
    if not df_trans.empty:
        total = df_trans['total_amount'].sum()
        total_8 = df_trans['amount_8_percent'].sum()
        latest_date = df_trans['date'].max()
        
        result_dict["transport"] = {
            "date": latest_date,
            "store_name": "交通費（電車・バス等）",
            "invoice_number": "", 
            "total_amount": total,
            "amount_8_percent": total_8
        }

    # --- 2. 駐車場 (parking) ---
    df_park = df[df['category'] == 'parking']
    if not df_park.empty:
        total = df_park['total_amount'].sum()
        total_8 = df_park['amount_8_percent'].sum()
        latest_date = df_park['date'].max()
        
        result_dict["parking"] = {
            "date": latest_date,
            "store_name": "駐車場代",
            "invoice_number": "", 
            "total_amount": total,
            "amount_8_percent": total_8
        }

    # --- 3. 高速代 (highway) ---
    df_high = df[df['category'] == 'highway']
    if not df_high.empty:
        total = df_high['total_amount'].sum()
        total_8 = df_high['amount_8_percent'].sum()
        latest_date = df_high['date'].max()
        
        result_dict["highway"] = {
            "date": latest_date,
            "store_name": "高速代",
            "invoice_number": "", 
            "total_amount": total,
            "amount_8_percent": total_8
        }

    # --- 4. 一般 (general) の集計と名寄せ ---
    # 上記のいずれでもないデータを抽出
    df_gen = df[~df['category'].isin(['transport', 'parking', 'highway'])]
    
    if not df_gen.empty:
        # 店舗名でグループ化して集計（名寄せ）
        grouped = df_gen.groupby('store_name').agg({
            'date': 'max',
            'total_amount': 'sum',
            'amount_8_percent': 'sum',
            'invoice_number': 'first'
        }).reset_index()

        general_list = []
        for _, row in grouped.iterrows():
            general_list.append({
                "date": row['date'],
                "store_name": row['store_name'],
                "invoice_number": row['invoice_number'],
                "total_amount": row['total_amount'],
                "amount_8_percent": row['amount_8_percent']
            })
        
        # 日付順ソート
        general_list.sort(key=lambda x: x.get("date") if x.get("date") else "9999/99/99")
        result_dict["general"] = general_list

    return result_dict

# --- メインロジック ---
def analyze_and_create_excel(uploaded_file, template_path, output_excel_path):
    api_key_to_use = API_KEY or st.secrets.get("GOOGLE_API_KEY")
    if not api_key_to_use:
        st.error("APIキー設定エラー")
        return None

    genai.configure(api_key=api_key_to_use)
    
    # ▼▼▼ プロンプト: 高速代カテゴリを追加し、交通費を厳格化 ▼▼▼
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        generation_config={"temperature": 0, "response_mime_type": "application/json"},
        system_instruction="""
        あなたは最高レベルの精度を持つ経理担当AIです。
        アップロードされたPDF（複数枚のレシート画像）から情報を抽出し、JSONデータを作成してください。
        
        ### 1. 店舗名の正規化 (store_name)
        - 支店名は削除し、会社名のみ抽出（例: "島忠 〇〇店" → "島忠"）。
        
        ### 2. カテゴリ判定 (category) - 以下の優先順位で判定してください
        
        **優先度A: 公共交通機関 (transport)**
        - **対象:** 電車、バス、地下鉄、モノレールのみ。
        - **キーワード:** 「乗車券」「切符」「運賃」「チャージ」「Suica」「PASMO」「JR」「駅」「交通局」「バス」。
        - ※高速道路やタクシーは含めないこと。
        
        **優先度B: 高速道路 (highway)**
        - **対象:** 高速道路の利用料金。
        - **キーワード:** 「ETC」「高速」「料金所」「通行料」「有料道路」「Highway」「首都高」。
        - 該当する場合、`highway` と判定してください。
        
        **優先度C: 駐車場 (parking)**
        - **対象:** 駐車料金。
        - **キーワード:** 「駐車場」「パーキング」「Parking」「Ｐ」「コインパーキング」。
        - **文脈:** 店名が不明でも「入庫」「出庫」「駐車時間」の記載があれば `parking` と判定。
        
        **優先度D: その他 (general)**
        - 上記以外（飲食、物品購入、タクシーなど）は `general` と判定。

        ### 3. 金額とインボイス
        - **date:** YYYY/MM/DD。
        - **total_amount:** 支払総額（税込）。
        - **amount_8_percent:** 「8%対象」等の記載がある金額。なければ 0。
        - **invoice_number:** T+13桁。なければ null。
        
        ### 出力フォーマット (JSON List)
        [{"status": "success", "date": "YYYY/MM/DD", "store_name": "...", "category": "general", "invoice_number": "T...", "total_amount": 1000, "amount_8_percent": 0}]
        """
    )

    try:
        temp_pdf_path = "temp_input.pdf"
        with open(temp_pdf_path, "wb") as f: f.write(uploaded_file.getbuffer())

        sample_file = genai.upload_file(path=temp_pdf_path, display_name="User Upload PDF")
        
        with st.spinner(f' Gemini {MODEL_NAME} で解析中... (電車・バス / 高速代 / 駐車場 を自動分類)'):
            while sample_file.state.name == "PROCESSING":
                time.sleep(1)
                sample_file = genai.get_file(sample_file.name)
            
            if sample_file.state.name == "FAILED": return None

            response = model.generate_content([sample_file, "全ページのレシート情報を抽出してください。"])
            raw_data = json.loads(response.text)

        # データの集計・分類
        analyzed_data = aggregate_receipt_data(raw_data)

        # Excel書き込み
        wb = openpyxl.load_workbook(template_path)
        ws = wb.active 
        
        # --- 書き込み用ヘルパー関数 ---
        def write_row(row_idx, item_data):
            if not item_data: return
            if item_data.get("date"): smart_write(ws, row_idx, 2, item_data["date"])
            if item_data.get("store_name"): smart_write(ws, row_idx, 5, item_data["store_name"])
            
            total = item_data.get("total_amount", 0)
            amt_8 = item_data.get("amount_8_percent", 0)
            amt_10_target = total - amt_8

            if amt_8 > 0: smart_write(ws, row_idx, 16, amt_8)
            if amt_10_target > 0: smart_write(ws, row_idx, 19, amt_10_target)

        # ▼▼▼ 書き込み位置の制御 ▼▼▼
        
        # 1. 公共機関 (9行目固定)
        if analyzed_data["transport"]:
            write_row(9, analyzed_data["transport"])
            
        # 2. 駐車場 (10行目固定)
        if analyzed_data["parking"]:
            write_row(10, analyzed_data["parking"])

        # 3. 11行目以降のリスト作成
        # 「高速代」がある場合、リストの先頭に追加する
        items_to_write = []
        if analyzed_data["highway"]:
            items_to_write.append(analyzed_data["highway"])
        
        items_to_write.extend(analyzed_data["general"])

        # 4. ループ書き込み (11行目からスタート)
        current_row = 11
        for item in items_to_write:
            # ページ跨ぎ処理: 30行目を超えたら41行目へジャンプ
            if current_row >= 30 and current_row < 41:
                current_row = 41
            
            write_row(current_row, item)
            current_row += 1

        wb.save(output_excel_path)
        
        # 結果表示用にリストを作成
        display_list = []
        if analyzed_data["transport"]: display_list.append(analyzed_data["transport"])
        if analyzed_data["parking"]: display_list.append(analyzed_data["parking"])
        if analyzed_data["highway"]: display_list.append(analyzed_data["highway"])
        display_list.extend(analyzed_data["general"])
        
        return display_list

    except Exception as e:
        st.error(f"システムエラー: {e}")
        return None

# --- UI実装 ---
if check_password():
    st.title("🧾 経費精算 AI (Ver.3.1 高速代対応)")
    st.caption(f"Powered by {MODEL_NAME}")
    st.markdown("---")
    
    col1, col2 = st.columns([1, 2.5])

    with col1:
        st.subheader("📂 ファイル選択")
        uploaded_file = st.file_uploader("PDFアップロード", type=["pdf"])
        if uploaded_file:
            st.success("準備完了")
            st.markdown("""
            **出力ルール:**
            - **09行目:** 交通費 (電車/バス)
            - **10行目:** 駐車場代
            - **11行目:** 高速代 (あれば先頭)
            - **11行目~:** その他 (店舗ごと)
            """)
            if st.button("読み取り開始", type="primary", use_container_width=True):
                if os.path.exists(TEMPLATE_FILE):
                    result = analyze_and_create_excel(uploaded_file, TEMPLATE_FILE, "result_download.xlsx")
                    if result:
                        st.session_state['result_data'] = result
                        st.session_state['excel_ready'] = True
                else: st.error("テンプレートが見つかりません")
            
            if 'excel_ready' in st.session_state:
                with open("result_download.xlsx", "rb") as f:
                    st.download_button("📥 Excelダウンロード", f, file_name="経費精算.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="secondary", use_container_width=True)

    with col2:
        st.subheader("📊 解析結果")
        if 'result_data' in st.session_state:
            data = st.session_state['result_data']
            total = sum([d.get("total_amount", 0) for d in data])
            st.metric("支払総額", f"¥{total:,}")
            
            df = pd.DataFrame(data)
            df["val_10"] = df["total_amount"] - df["amount_8_percent"]
            
            # アイコン表示 (高速代を追加)
            def get_icon(cat_name):
                s = str(cat_name)
                if "交通費" in s: return "🚆"
                if "駐車場" in s: return "🅿️"
                if "高速代" in s: return "🛣️" # Highway icon
                return "🛒"

            df["Type"] = df["store_name"].apply(get_icon)
            
            st.dataframe(
                df[["Type", "date", "store_name", "total_amount", "val_10", "amount_8_percent"]].rename(columns={"date":"日付","store_name":"項目/店舗名","total_amount":"総額","val_10":"10%","amount_8_percent":"8%"}),
                use_container_width=True, hide_index=True
            )
        else:
            st.info("左のボタンで実行してください")