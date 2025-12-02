import google.generativeai as genai
import os
from dotenv import load_dotenv

# .envファイルを読み込む
load_dotenv()

# APIキーの設定
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("❌ エラー: APIキーが見つかりません。.envファイルを確認してください。")
else:
    try:
        genai.configure(api_key=api_key)
        print("--- 🔍 利用可能なモデル一覧 ---")
        
        # モデル一覧を取得して表示
        for m in genai.list_models():
            # テキスト生成に対応しているモデルのみ抽出
            if 'generateContent' in m.supported_generation_methods:
                print(f"・ {m.name}")
        
        print("\n--- 完了 ---")
        
    except Exception as e:
        print(f"❌ 通信エラー: {e}")