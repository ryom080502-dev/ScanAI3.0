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

# ▼▼▼ モデル変更: gemini-2.5-pro を指定 ▼▼▼
MODEL_NAME = "gemini-2.5-pro"
TEMPLATE_FILE = "template.xlsx"

# ▼▼▼ 合言葉の設定 ▼▼▼
LOGIN_PASSWORD = "fujishima8888" 
# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

# --- ページ設定 ---
st.set_page_config(page_title="経費精算AI (Ver.2.5 Pro)", layout="wide")

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

# --- ▼▼▼ 集計・分類ロジック ▼▼▼ ---
def aggregate_receipt_data(raw_data):
    """
    データを「交通費」「駐車場」「一般」の3つに分類して集計する
    """
    df = pd.DataFrame(raw_data)
    if df.empty: return {"transport": None, "parking": None, "general": []}

    # 数値変換
    cols_to_num = ['total_amount', 'amount_8_percent']
    for col in cols_to_num:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    result_dict = {
        "transport": None, # 9行目用
        "parking": None,   # 10行目用
        "general": []      # 11行目以降用
    }

    # --- 1. 交通費 (transport) の集計 ---
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

    # --- 2. 駐車場 (parking) の集計 ---
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

    # --- 3. 一般 (general) の集計と名寄せ ---
    df_gen = df[(df['category'] != 'transport') & (df['category'] != 'parking')]
    
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
    
    # ▼▼▼ プロンプト: Gemini 2.5 Pro 向けに最適化 ▼▼▼
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        generation_config={"temperature": 0, "response_mime_type": "application/json"},
        system_instruction="""
        あなたは最高レベルの精度を持つ経理担当AIです。
        アップロードされたPDF（複数枚のレシート画像）から情報を抽出し、JSONデータを作成してください。
        Gemini 2.5 Proの高度な視覚認識能力を活用し、かすれた文字や文脈からも正確に情報を読み取ってください。
        
        ### 1. 店舗名の正規化 (store_name)
        - 支店名は削除し、会社名のみ抽出してください（例: "島忠 〇〇店" → "島忠"）。
        - 駐車場で店名がない場合、無理に推測せず空白または「駐車場」としてください。
        
        ### 2. カテゴリ判定 (category) - 重要
        以下の優先順位でカテゴリを決定してください。
        
        **優先度A: 公共交通機関 (transport)**
        - キーワード: 「駅」「切符」「乗車券」「運賃」「チャージ」「Suica」「PASMO」「JR」「地下鉄」「バス」「交通局」。
        - 該当する場合、必ず `transport` と判定。
        
        **優先度B: 駐車場 (parking)**
        - キーワード: 「駐車場」「パーキング」「Parking」「Ｐ」「コインパーキング」。
        - **文脈判定:** 店名に「駐車場」がなくても、以下の情報があれば `parking` と判定してください。
          - 「入庫」「出庫」「入庫時刻」「精算時刻」「駐車時間」「No.（車室番号）」の記載がある。
          - 「駐車料金」「一時利用」などの品目がある。
        
        **優先度C: その他 (general)**
        - 上記以外（飲食、物品購入など）は `general` と判定。

        ### 3. 金額とインボイス
        - **date:** YYYY/MM/DD 形式。
        - **invoice_number:** Tから始まる13桁の番号。なければ null。
        - **total_amount:** 支払総額（税込）。
        - **amount_8_percent:** 「8%対象」「軽減税率」と明記されている金額のみ抽出。なければ 0。
        
        ### 出力フォーマット (JSON List)
        [{"status": "success", "date": "YYYY/MM/DD", "store_name": "...", "category": "general", "invoice_number": "T...", "total_amount": 1000, "amount_8_percent": 0}]
        """
    )

    try:
        temp_pdf_path = "temp_input.pdf"
        with open(temp_pdf_path, "wb") as f: f.write(uploaded_file.getbuffer())

        sample_file = genai.upload_file(path=temp_pdf_path, display_name="User Upload PDF")
        
        with st.spinner(f' Gemini {MODEL_NAME} で超高精度解析中... (交通費・駐車場・その他を自動分類)'):
            # ファイル処理待ち
            while sample_file.state.name == "PROCESSING":
                time.sleep(1)
                sample_file = genai.get_file(sample_file.name)
            
            if sample_file.state.name == "FAILED":
                st.error("Google側でのファイル処理に失敗しました")
                return None

            # 解析実行
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

            # 8%欄 (P列: 16列目)
            if amt_8 > 0: smart_write(ws, row_idx, 16, amt_8)
            # 10%欄 (S列: 19列目) - インボイス有無に関わらず基本ここへ
            if amt_10_target > 0: smart_write(ws, row_idx, 19, amt_10_target)

        # ▼▼▼ 書き込み位置の制御 ▼▼▼
        
        # 1. 公共機関 (9行目固定)
        if analyzed_data["transport"]:
            write_row(9, analyzed_data["transport"])
            
        # 2. 駐車場 (10行目固定)
        if analyzed_data["parking"]:
            write_row(10, analyzed_data["parking"])

        # 3. その他 (11行目以降)
        current_row = 11
        for item in analyzed_data["general"]:
            # ページ跨ぎ処理: 30行目を超えたら41行目へジャンプ (テンプレート依存)
            if current_row >= 30 and current_row < 41:
                current_row = 41
            
            write_row(current_row, item)
            current_row += 1

        wb.save(output_excel_path)
        
        # 結果表示用にリストをフラットにして返す
        display_list = []
        if analyzed_data["transport"]: display_list.append(analyzed_data["transport"])
        if analyzed_data["parking"]: display_list.append(analyzed_data["parking"])
        display_list.extend(analyzed_data["general"])
        
        return display_list

    except Exception as e:
        st.error(f"システムエラー: {e}")
        return None

# --- UI実装 ---
if check_password():
    st.title("🧾 経費精算 AI (Ver.2.5 Pro)")
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
            - **9行目:** 交通費 (電車/バス) 合計
            - **10行目:** 駐車場代 合計 (入出庫時間で自動判定)
            - **11行目~:** 店舗ごとの明細 (自動名寄せ)
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
            
            # アイコン表示
            def get_icon(cat_name):
                if "交通費" in str(cat_name): return "🚆"
                if "駐車場" in str(cat_name): return "🅿️"
                return "🛒"

            # 表示用に store_name からアイコンを判定 (集計後は store_name にカテゴリ名が入っているため)
            df["Type"] = df["store_name"].apply(get_icon)
            
            st.dataframe(
                df[["Type", "date", "store_name", "total_amount", "val_10", "amount_8_percent"]].rename(columns={"date":"日付","store_name":"項目/店舗名","total_amount":"総額","val_10":"10%","amount_8_percent":"8%"}),
                use_container_width=True, hide_index=True
            )
        else:
            st.info("左のボタンで実行してください")