import os
import sys
import re
import json
import logging
import argparse
import textwrap

import openai

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(funcName)s() - %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
    level=logging.INFO,
)


# ========== 基礎 I/O 工具 ==========

def read_json(file, write_log=False):
    if write_log:
        logger.info(f"Reading {file}")
    with open(file, "r", encoding="utf8") as f:
        data = json.load(f)
    if write_log:
        if isinstance(data, dict):
            logger.info(f"Read dict with {len(data)} keys")
        elif isinstance(data, list):
            logger.info(f"Read list with {len(data)} elements")
    return data


def write_json(file, data, indent=None, write_log=False):
    if write_log:
        if isinstance(data, dict):
            logger.info(f"Writing dict with {len(data)} keys to {file}")
        elif isinstance(data, list):
            logger.info(f"Writing list with {len(data)} elements to {file}")
    with open(file, "w", encoding="utf8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    if write_log:
        logger.info("Written")


def write_txt(file, text, write_log=False):
    if write_log:
        logger.info(f"Writing text to {file} ({len(text)} chars)")
    with open(file, "w", encoding="utf8") as f:
        f.write(text)
    if write_log:
        logger.info("Written")


def print_box(text: str):
    print("\n" + "\n".join(textwrap.wrap(str(text), width=70)) + "\n")


# ========== Config：全部從 config.json 讀 ==========

class Config:
    def __init__(self, config_file: str):
        data = read_json(config_file, write_log=True)

        # 建議設成 "gpt-3.5-turbo"
        self.model = data["model"]
        self.prompts = data["prompts"]   # start / opening / explore / quiz / ending
        self.rooms = data["rooms"]       # 地圖：每個房間 + connections

        # 所有輸出都放在 lab2_output 底下
        self.output_dir = "lab2_output"
        os.makedirs(self.output_dir, exist_ok=True)


# ========== GPT 包裝：使用舊版 ChatCompletion API ==========

class GPT:
    def __init__(self, model: str):
        # 若 config 沒給就 fallback gpt-3.5-turbo
        self.model = model or "gpt-3.5-turbo"

    def run(self, prompt: str, max_tokens: int = 800) -> str:
        logger.info("Calling OpenAI ChatCompletion...")
        resp = openai.ChatCompletion.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            n=1,
        )
        return resp["choices"][0]["message"]["content"]


# ========== 遊戲 State ==========

class State:
    def __init__(self, save_file: str = ""):
        self.save_file = save_file

        # 日誌（之後拿來做 summary）
        self.log = ""

        # 進度
        self.turn = 0
        self.chapter = 1
        self.location = "bunker_entrance"

        # 玩家設定
        self.profession = "hardware"   # hardware / software / control / design
        self.level = 1
        self.hp = 3

        # 機器人建造進度
        self.knowledge_score = 0
        self.robot_parts = {
            "power": False,
            "motor": False,
            "sensors": False,
            "control": False,
        }

        # 探索 / 彩蛋
        self.flags = {}
        self.inventory = []

        # 末日壓力感
        self.danger_level = 10   # 0~100

        # 結束狀態
        self.is_game_over = False
        self.is_win = False

    def to_dict(self):
        return {
            "log": self.log,
            "turn": self.turn,
            "chapter": self.chapter,
            "location": self.location,
            "profession": self.profession,
            "level": self.level,
            "hp": self.hp,
            "knowledge_score": self.knowledge_score,
            "robot_parts": self.robot_parts,
            "flags": self.flags,
            "inventory": self.inventory,
            "danger_level": self.danger_level,
            "is_game_over": self.is_game_over,
            "is_win": self.is_win,
        }

    def save(self):
        if not self.save_file:
            return
        write_json(self.save_file, self.to_dict(), indent=2, write_log=True)

    def load(self):
        if not self.save_file or not os.path.exists(self.save_file):
            return
        data = read_json(self.save_file, write_log=True)
        self.log = data.get("log", "")
        self.turn = data.get("turn", 0)
        self.chapter = data.get("chapter", 1)
        self.location = data.get("location", "bunker_entrance")
        self.profession = data.get("profession", "hardware")
        self.level = data.get("level", 1)
        self.hp = data.get("hp", 3)
        self.knowledge_score = data.get("knowledge_score", 0)
        self.robot_parts = data.get("robot_parts", {
            "power": False,
            "motor": False,
            "sensors": False,
            "control": False,
        })
        self.flags = data.get("flags", {})
        self.inventory = data.get("inventory", [])
        self.danger_level = data.get("danger_level", 10)
        self.is_game_over = data.get("is_game_over", False)
        self.is_win = data.get("is_win", False)


# ========== 主 Game 類別 ==========

class Game:
    def __init__(self, config: Config):
        self.config = config
        self.gpt = GPT(config.model)
        self.prompts = config.prompts
        self.rooms = config.rooms
        self.output_dir = config.output_dir

        self.max_saves = 4
        self.state = State()
        self.summary_file = ""

        # 給 LLM 用
        self.last_action_id = ""
        self.last_free_text = ""

    # ---------- 遊戲開始 ----------

    def run_start(self):
        # 讀 start prompt
        start_prompt = self.prompts.get(
            "start",
            "開始新遊戲(1) / 載入存檔(2)： "
        )

        while True:
            text_in = input(start_prompt)
            if text_in == "1":
                start_type = "new"
                break
            elif text_in == "2":
                start_type = "load"
                break

        # 存檔欄位（全部放在 lab2_output）
        save_list_text = "\n存檔列表：\n"
        saveid_to_exist = {}
        for i in range(self.max_saves):
            save_id = str(i + 1)
            save_file = os.path.join(self.output_dir, f"save_{save_id}.json")
            if os.path.exists(save_file):
                saveid_to_exist[save_id] = True
                save_list_text += f"({save_id}) 舊有存檔\n"
            else:
                saveid_to_exist[save_id] = False
                save_list_text += f"({save_id}) 空白存檔\n"

        use_save_id = ""
        while True:
            text_in = input(save_list_text + "\n使用存檔欄位： ")
            if start_type == "new":
                if text_in in saveid_to_exist:
                    use_save_id = text_in
                    break
            else:
                if saveid_to_exist.get(text_in, False):
                    use_save_id = text_in
                    break

        save_file = os.path.join(self.output_dir, f"save_{use_save_id}.json")
        self.summary_file = os.path.join(self.output_dir, f"summary_{use_save_id}.txt")
        self.state = State(save_file)

        if start_type == "new":
            self.choose_profession()
            opening = self.prompts.get("opening", "世界末日，你在地下室醒來……")
            input(opening + "\n(按 ENTER 開始冒險)... ")
            self.state.log += opening + "\n"
            self.state.save()
        else:
            self.state.load()

        self.run_loop()

    def choose_profession(self):
        text = (
            "選擇你的背景職業：\n"
            "1. 硬體工程（hardware）\n"
            "2. 軟體工程（software）\n"
            "3. 控制工程（control）\n"
            "4. 設計 / UX（design）\n\n"
            "輸入數字選擇職業："
        )
        mapping = {
            "1": "hardware",
            "2": "software",
            "3": "control",
            "4": "design",
        }
        while True:
            ans = input(text).strip()
            if ans in mapping:
                self.state.profession = mapping[ans]
                break

    # ---------- 主 loop ----------

    def run_loop(self):
        while True:
            if self.state.is_game_over or self.state.is_win:
                break

            self.state.turn += 1

            # 60% explore, 40% quiz（簡單用 turn 控）
            if self.state.turn % 3 == 0:
                mode = "quiz"
            else:
                mode = "explore"

            if mode == "quiz":
                turn_data = self.run_quiz_turn()
            else:
                turn_data = self.run_explore_turn()

            if not turn_data:
                print_box("LLM 回應解析失敗，遊戲結束 QQ")
                break

            # 敘事
            narration = turn_data.get("narration", "")
            print_box(f"[回合 {self.state.turn}]")
            print_box(narration)
            self.state.log += f"\n[Turn {self.state.turn}]\n{narration}\n"

            # 媒體 prompt
            media = turn_data.get("media") or {}
            img_p = media.get("image_prompt")
            aud_p = media.get("audio_prompt")
            if img_p:
                print_box("🎨 圖片生成提示：\n" + img_p)
            if aud_p:
                print_box("🎵 音效/音樂生成提示：\n" + aud_p)

            quiz_result = None

            if turn_data.get("mode") == "quiz" and turn_data.get("quiz"):
                quiz_result = self.handle_quiz(turn_data)
            else:
                self.handle_explore(turn_data)

            # 更新 state（不信任 LLM 的 location）
            state_hint = turn_data.get("state_update_hint") or {}
            self.apply_state_update(state_hint, quiz_result)

            # 顯示狀態
            print_box(
                f"狀態：HP={self.state.hp} 等級={self.state.level} "
                f"知識點={self.state.knowledge_score} 危險度={self.state.danger_level}\n"
                f"位置：{self.state.location}\n"
                f"機器人部件：{self.state.robot_parts}\n"
                f"道具：{self.state.inventory}"
            )

            # 勝利條件：四模組完成
            if all(self.state.robot_parts.values()):
                self.state.is_win = True

            # HP 歸零
            if self.state.hp <= 0:
                self.state.is_game_over = True

            # 存檔
            self.state.save()

        # 結局 + 摘要
        self.do_ending()

    # ---------- LLM: explore 回合（只敘事 + 非移動互動） ----------

    def run_explore_turn(self):
        tmpl = self.prompts.get("explore", "")
        room_info = self.rooms.get(self.state.location, {})
        rooms_json = json.dumps(self.rooms, ensure_ascii=False)

        state_json = json.dumps({
            "turn": self.state.turn,
            "chapter": self.state.chapter,
            "location": self.state.location,
            "profession": self.state.profession,
            "level": self.state.level,
            "hp": self.state.hp,
            "knowledge_score": self.state.knowledge_score,
            "robot_parts": self.state.robot_parts,
            "flags": self.state.flags,
            "inventory": self.state.inventory,
            "danger_level": self.state.danger_level,
        }, ensure_ascii=False)

        room_json = json.dumps(room_info, ensure_ascii=False)
        action_text = self.last_action_id or ""

        prompt = tmpl.replace("{state_json}", state_json)\
                     .replace("{action_text}", action_text)\
                     .replace("{room_json}", room_json)\
                     .replace("{rooms_json}", rooms_json)

        out = self.gpt.run(prompt, max_tokens=800)
        try:
            turn_data = json.loads(out)
        except json.JSONDecodeError:
            logger.error("Explore JSON parse error")
            logger.error(out)
            return None

        # 安全起見：過濾掉 LLM 亂生的 move_* 選項
        choices = turn_data.get("choices") or []
        filtered = []
        for c in choices:
            cid = c.get("id", "")
            if isinstance(cid, str) and cid.startswith("move_"):
                # 忽略 LLM 生成的移動選項
                continue
            filtered.append(c)
        turn_data["choices"] = filtered

        # 不相信 LLM 的 location 更新
        if "state_update_hint" in turn_data and isinstance(turn_data["state_update_hint"], dict):
            turn_data["state_update_hint"].pop("location", None)

        return turn_data

    # ---------- LLM: quiz 回合 ----------

    def run_quiz_turn(self):
        tmpl = self.prompts.get("quiz", "")
        state_json = json.dumps({
            "turn": self.state.turn,
            "chapter": self.state.chapter,
            "location": self.state.location,
            "profession": self.state.profession,
            "level": self.state.level,
            "hp": self.state.hp,
            "knowledge_score": self.state.knowledge_score,
            "robot_parts": self.state.robot_parts,
            "flags": self.state.flags,
            "inventory": self.state.inventory,
            "danger_level": self.state.danger_level,
        }, ensure_ascii=False)

        prompt = tmpl.replace("{state_json}", state_json)
        out = self.gpt.run(prompt, max_tokens=800)
        try:
            turn_data = json.loads(out)
        except json.JSONDecodeError:
            logger.error("Quiz JSON parse error")
            logger.error(out)
            return None

        # 同樣不接受 LLM 改 location
        if "state_update_hint" in turn_data and isinstance(turn_data["state_update_hint"], dict):
            turn_data["state_update_hint"].pop("location", None)

        return turn_data

    # ---------- 產生「移動選項」：完全由程式根據 connections 決定 ----------

    def get_movement_choices(self):
        room = self.rooms.get(self.state.location, {})
        conns = room.get("connections", [])
        moves = []
        for conn in conns:
            # 顯示名稱可以自己美化，這裡先顯示房間 key
            text = f"前往 {conn}"
            moves.append({"id": f"move_{conn}", "text": text})
        return moves

    # ---------- 處理 quiz ----------

    def handle_quiz(self, turn_data):
        q = turn_data["quiz"]
        print_box("[教學題] " + q["question"])
        for key, text in q["options"].items():
            print(f"  {key}. {text}")

        while True:
            ans = input("\n你的選擇 (A/B/C/D)，或輸入 S 存檔：").strip().upper()
            if ans == "S":
                self.state.save()
                continue
            if ans in ["A", "B", "C", "D"]:
                break
            print("輸入錯誤，請重新輸入。")

        correct = q["correct"].upper()
        if ans == correct:
            print_box("✔ 答對了！\n" + q["explanation"])
            quiz_result = "correct"
        else:
            print_box(f"✘ 答錯了，正確答案是 {correct}\n" + q["explanation"])
            quiz_result = "wrong"

        self.last_action_id = f"quiz_answer_{ans}"
        self.last_free_text = ""
        self.state.log += f"\n[Quiz] Q: {q['question']}\nAns: {ans}, Correct: {correct}\n"
        return quiz_result

    # ---------- 處理 explore：LLM 選項 + 程式產生的移動選項 ----------

    def handle_explore(self, turn_data):
        llm_choices = turn_data.get("choices") or []
        move_choices = self.get_movement_choices()

        # 合併（先 LLM 互動，再移動）
        choices = llm_choices + move_choices

        if not choices:
            print_box("沒有選項可選，這回合略過。")
            self.last_action_id = ""
            self.last_free_text = ""
            return

        print("可選行動：")
        for idx, c in enumerate(choices, start=1):
            print(f"{idx}. {c['text']}")
        print("S. 存檔")
        print("Q. 離開遊戲")

        chosen = None
        while True:
            sel = input("\n你的選擇：").strip()
            if sel.upper() == "S":
                self.state.save()
                continue
            if sel.upper() == "Q":
                print_box("你選擇暫時離開這座實驗大樓。")
                self.state.is_game_over = True
                return
            try:
                idx = int(sel) - 1
                if 0 <= idx < len(choices):
                    chosen = choices[idx]
                    break
            except ValueError:
                pass
            print("輸入錯誤，請重新輸入。")

        cid = chosen["id"]

        # 若是移動選項：完全由程式處理、LLM 不參與 location 變更
        if cid.startswith("move_"):
            new_loc = cid.replace("move_", "")
            # 檢查是否真的是合法連接
            room = self.rooms.get(self.state.location, {})
            if new_loc in room.get("connections", []):
                old_loc = self.state.location
                self.state.location = new_loc
                self.last_action_id = cid
                self.last_free_text = ""
                self.state.log += f"\n[移動] 從 {old_loc} 前往 {new_loc}\n"
            else:
                # 理論上不會發生，安全起見防一下
                self.state.log += f"\n[移動失敗] 無效連接 {cid}\n"
            return

        # 若是自由輸入行動
        if cid == "free_action":
            free_text = input("請自由描述你想做的行動：")
            self.last_action_id = "free_action"
            self.last_free_text = free_text
            self.state.log += f"\n[自由行動] {free_text}\n"
            return

        # 否則是一般 LLM 行動
        self.last_action_id = cid
        self.last_free_text = ""
        self.state.log += f"\n[選項] {chosen['text']}\n"

    # ---------- 更新 State：不接受 LLM 改 location ----------

    def apply_state_update(self, hint: dict, quiz_result: str | None):
        if not isinstance(hint, dict):
            hint = {}

        # 完全忽略 hint["location"]，避免 LLM 瞬間移動
        hint.pop("location", None)

        chapter = hint.get("chapter")
        if chapter:
            self.state.chapter = int(chapter)

        robot_parts = hint.get("robot_parts") or {}
        for k in self.state.robot_parts.keys():
            if k in robot_parts and isinstance(robot_parts[k], bool):
                self.state.robot_parts[k] = robot_parts[k]

        flags = hint.get("flags") or {}
        for k, v in flags.items():
            self.state.flags[k] = v

        inv_add = hint.get("inventory_add") or []
        for item in inv_add:
            if item not in self.state.inventory:
                self.state.inventory.append(item)

        danger_delta = int(hint.get("danger_delta") or 0)
        self.state.danger_level = max(0, min(100, self.state.danger_level + danger_delta))

        knowledge_delta = int(hint.get("knowledge_delta") or 0)
        self.state.knowledge_score += knowledge_delta

        # quiz 額外處理
        if quiz_result == "correct":
            self.state.knowledge_score += 1
        elif quiz_result == "wrong":
            self.state.hp -= 1

        hp_delta = int(hint.get("hp_delta") or 0)
        self.state.hp += hp_delta

        # 升級規則（簡單版）
        if self.state.knowledge_score in (3, 6):
            self.state.level += 1

    # ---------- 結局 & 摘要 ----------

    def do_ending(self):
        if self.state.is_win:
            ending = self.prompts.get("ending", "你完成了求生機器人，走向未知世界。")
            input(ending + "\n(按 ENTER 生成旅程總結)... ")
            self.state.log += "\n[Ending]\n" + ending + "\n"
        elif self.state.is_game_over:
            text = "你在這座末日實驗大樓中失去了行動能力。\n也許下一次，你能做出更好的選擇。"
            input(text + "\n(按 ENTER 生成旅程總結)... ")
            self.state.log += "\n[Game Over]\n" + text + "\n"
        else:
            text = "你暫時離開了這座實驗大樓。"
            input(text + "\n(按 ENTER 生成旅程總結)... ")
            self.state.log += "\n[Exit]\n" + text + "\n"

        # 摘要：用同一個 model 生成
        story = re.sub(r"\n+", "\n", self.state.log).strip()
        instruction = (
            "請將以下遊戲歷程整理成一篇中文短文，約 15~25 句話，"
            "描述玩家在末日機器人實驗大樓中的冒險，以及學到的機器人相關知識。"
        )
        prompt = f"{instruction}\n\n遊戲歷程：\n{story}"
        try:
            summary = self.gpt.run(prompt, max_tokens=800)
        except Exception as e:
            logger.error(f"Summary generation failed: {e}")
            summary = story

        summary = re.sub(r"\n+", "\n", summary).strip()
        write_txt(self.summary_file, summary, write_log=True)
        print_box("本次旅程總結：\n" + summary)
        input("\n(按 ENTER 結束遊戲)... ")


# ========== main ==========

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config_file", type=str, default="config.json")
    args = parser.parse_args()

    # 要你輸入 sk- 的 key
    openai.api_key = input("OpenAI API Key: ").strip()

    cfg = Config(args.config_file)
    game = Game(cfg)
    game.run_start()


if __name__ == "__main__":
    main()
    sys.exit()
