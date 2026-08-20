import asyncio
import json
import random
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Токен не найден! Проверьте файл .env")

BALANCE_FILE = "balance.txt"
INVENTORY_FILE = "inventory.json"
LOANS_FILE = "loans.json"
MIN_BET = 50
DEFAULT_BALANCE = 100

router = Router()


# ---------- Менеджер Ключевой Ставки ЦБ ----------

class KeyRateManager:
    rate = 16.0  # Начальная ставка

    @classmethod
    def update_rate(cls):
        # Ставка колеблется от 1% до 50%
        change = random.randint(-4, 4)
        cls.rate = max(1.0, min(50.0, cls.rate + change))

    @classmethod
    def get_rate(cls):
        return round(cls.rate, 1)


# ---------- Состояния FSM ----------

class Form(StatesGroup):
    casino_bet = State()
    blackjack_bet = State()
    invest_amount = State()
    sell_item = State()
    microloan_amount = State()
    credit_amount = State()
    mortgage_amount = State()


# ---------- Баланс ----------

class BalanceManager:
    @staticmethod
    def load_balance(user_id, default_balance=DEFAULT_BALANCE):
        try:
            with open(f"{user_id}_{BALANCE_FILE}", "r", encoding="utf-8") as f:
                return int(float(f.read().strip()))
        except (OSError, ValueError):
            return default_balance

    @staticmethod
    def save_balance(user_id, balance):
        with open(f"{user_id}_{BALANCE_FILE}", "w", encoding="utf-8") as f:
            f.write(str(int(balance)))


# ---------- Кредиты ----------

class LoanManager:
    def __init__(self, user_id):
        self.user_id = user_id
        self.filename = f"{self.user_id}_{LOANS_FILE}"

    def load_loans(self):
        try:
            if Path(self.filename).exists():
                with open(self.filename, "r", encoding="utf-8") as f:
                    return json.load(f)
            return []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def save_loans(self, loans):
        with open(self.filename, "w", encoding="utf-8") as f:
            json.dump(loans, f, ensure_ascii=False, indent=2)

    def get_loans_count(self):
        return len(self.load_loans())

    def take_loan(self, loan_type: str, amount: int, interest_rate: float):
        loans = self.load_loans()
        if len(loans) >= 3:
            return False, "🛑 Достигнут лимит! Можно иметь максимум 3 активных кредита.", 0

        total_due = int(amount * (1 + interest_rate / 100))
        loans.append({
            "type": loan_type,
            "principal": amount,
            "rate": interest_rate,
            "total_due": total_due,
            "date": datetime.now().strftime("%d.%m.%Y %H:%M")
        })
        self.save_loans(loans)

        balance = BalanceManager.load_balance(self.user_id)
        BalanceManager.save_balance(self.user_id, balance + amount)

        return True, f"✅ Вы успешно взяли {loan_type.lower()} на {amount} монет под {interest_rate}%!\nК возврату: {total_due} монет.", total_due

    def pay_all_loans(self):
        loans = self.load_loans()
        if not loans:
            return False, "📭 У вас нет активных кредитов!"

        total_due = sum(item["total_due"] for item in loans)
        balance = BalanceManager.load_balance(self.user_id)

        if balance < total_due:
            return False, f"❌ Недостаточно средств для погашения всех кредитов!\nСумма долга: {total_due} монет.\nВаш баланс: {balance} монет."

        new_balance = balance - total_due
        BalanceManager.save_balance(self.user_id, new_balance)
        self.save_loans([])

        return True, f"🎉 Все кредиты успешно погашены на сумму {total_due} монет!\nОстаток баланса: {new_balance} монет."

    def get_loans_text(self):
        loans = self.load_loans()
        key_rate = KeyRateManager.get_rate()
        text = f"🏛 **Кредитный отдел** (КС ЦБ: {key_rate}%)\n\n"

        if not loans:
            text += "📭 У вас нет активных кредитов (Доступно: 3/3).\n"
        else:
            total_debt = sum(item["total_due"] for item in loans)
            text += f"📜 **Ваши активные кредиты ({len(loans)}/3):**\n"
            for i, item in enumerate(loans, 1):
                text += f"{i}. {item['type']} | Взято: {item['principal']} | Долг: {item['total_due']} ({item['rate']}%)\n"
            text += f"\n💰 **Общий долг:** {total_debt} монет\n"

        return text


# ---------- Инвентарь ----------

class Inventory:
    def __init__(self, user_id):
        self.user_id = user_id
        self.items = []

    def load_inventory(self):
        try:
            filename = f"{self.user_id}_{INVENTORY_FILE}"
            if Path(filename).exists():
                with open(filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        normalized_items = []
                        for item in data:
                            if isinstance(item, dict):
                                name = item.get("name") or item.get("resource") or "Unknown"
                                amount = item.get("amount") or item.get("quantity") or 0
                                purchase_price = item.get("purchase_price") or item.get("price") or 0
                                current_price = item.get("current_price") or purchase_price
                                normalized_items.append({
                                    "name": name,
                                    "amount": int(amount),
                                    "purchase_price": int(purchase_price),
                                    "current_price": int(current_price),
                                    "last_update": item.get("last_update", datetime.now().isoformat()),
                                })
                        return normalized_items
            return []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def save_inventory(self):
        filename = f"{self.user_id}_{INVENTORY_FILE}"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)

    def add_item(self, currency_name, amount, price):
        self.items = self.load_inventory()
        for existing_item in self.items:
            if existing_item["name"] == currency_name and existing_item["purchase_price"] == price:
                existing_item["amount"] += amount
                existing_item["current_price"] = price
                existing_item["last_update"] = datetime.now().isoformat()
                self.save_inventory()
                return True
        self.items.append({
            "name": currency_name,
            "amount": amount,
            "purchase_price": price,
            "current_price": price,
            "last_update": datetime.now().isoformat(),
        })
        self.save_inventory()
        return True

    def update_item_prices(self, market_prices):
        self.items = self.load_inventory()
        updated = False
        current_time = datetime.now()

        for item in self.items:
            currency_name = item["name"]
            if currency_name in market_prices:
                old_price = item["current_price"]
                new_price = market_prices[currency_name]

                if old_price != new_price:
                    item["current_price"] = new_price
                    item["last_update"] = current_time.isoformat()
                    updated = True

        if updated:
            self.save_inventory()

        return updated

    def sell_item(self, item_index, amount_to_sell):
        self.items = self.load_inventory()
        if item_index < 0 or item_index >= len(self.items):
            return None, 0, 0, 0

        item = self.items[item_index]
        if amount_to_sell <= 0 or amount_to_sell > item["amount"]:
            return None, 0, 0, 0

        current_price = item["current_price"]
        total_sale = amount_to_sell * current_price
        purchase_cost = amount_to_sell * item["purchase_price"]
        profit_loss = total_sale - purchase_cost
        item_name = item["name"]

        if amount_to_sell == item["amount"]:
            self.items.pop(item_index)
        else:
            item["amount"] -= amount_to_sell

        self.save_inventory()
        return item_name, total_sale, profit_loss, current_price

    def sell_all_items(self):
        self.items = self.load_inventory()
        if not self.items:
            return 0, 0

        total_sale = 0
        total_profit_loss = 0

        for item in self.items:
            amount = item["amount"]
            current_price = item["current_price"]
            purchase_price = item["purchase_price"]
            total_sale += amount * current_price
            total_profit_loss += (amount * current_price) - (amount * purchase_price)

        self.items = []
        self.save_inventory()
        return total_sale, total_profit_loss

    def get_inventory_text(self):
        self.items = self.load_inventory()
        if not self.items:
            return "📭 Ваш инвентарь пуст!"

        text = "🎒 ВАШ ИНВЕНТАРЬ: \n"

        for i, item in enumerate(self.items, 1):
            current_value = item["amount"] * item["current_price"]
            purchase_value = item["amount"] * item["purchase_price"]

            profit_loss = current_value - purchase_value
            profit_percent = (profit_loss * 100 // purchase_value) if purchase_value > 0 else 0

            arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"

            text += f"{i}. {item['name'].upper()} | {item['amount']} ед. | П: {item['purchase_price']} -> Т: {item['current_price']} | {arrow} {profit_loss:+d} ({profit_percent:+d}%)\n"

        return text


# ---------- Казино ----------

class CasinoGame:
    def __init__(self, user_id):
        self.user_id = user_id
        self.payouts = [
            {"chance": 5, "multiplier": 10, "message": "Just lucky... no more"},
            {"chance": 15, "multiplier": 3, "message": "Неплохо"},
            {"chance": 35, "multiplier": 2, "message": "Сойдет"},
            {"chance": 45, "multiplier": 0, "message": "Лох"},
        ]

    def play_round(self, bet, all_in=False):
        balance = BalanceManager.load_balance(self.user_id)

        if bet <= 0:
            return False, "Ставка должна быть положительной!", balance
        if bet < MIN_BET:
            return False, f"Минимальная ставка - {MIN_BET} монет!", balance
        if balance < bet:
            return False, "Недостаточно средств!", balance

        chance = random.randint(1, 100)
        cumulative_chance = 0
        selected_payout = self.payouts[-1]

        for payout in self.payouts:
            cumulative_chance += payout["chance"]
            if chance <= cumulative_chance:
                selected_payout = payout
                break

        win = (bet // 4) if selected_payout["multiplier"] == 0 else (bet * selected_payout["multiplier"])
        message = selected_payout["message"]
        net_result = win - bet
        new_balance = balance + net_result
        BalanceManager.save_balance(self.user_id, new_balance)

        prefix = "ALL IN! " if all_in else ""
        return True, f"{prefix}{message} | Ставка: {bet} | Выигрыш: {win} | Баланс: {new_balance}", new_balance

    def play_all_in(self):
        balance = BalanceManager.load_balance(self.user_id)
        if balance <= 0:
            return False, "Баланс пуст!", balance
        if balance < MIN_BET:
            return False, f"Мин. ставка - {MIN_BET}!", balance
        return self.play_round(balance, all_in=True)


# ---------- Игровой автомат ----------

class SlotMachineGame:
    SPIN_COST = 500
    SYMBOLS = ("💰", "💎", "🟢")
    CURRENCIES = ("доллар", "фунт стерлингов", "биткоин")

    def __init__(self, user_id):
        self.user_id = user_id

    def spin(self):
        balance = BalanceManager.load_balance(self.user_id)
        if balance < self.SPIN_COST:
            return False, f"Недостаточно монет! Стоимость: {self.SPIN_COST} монет."

        BalanceManager.save_balance(self.user_id, balance - self.SPIN_COST)
        reels = [random.choice(self.SYMBOLS) for _ in range(3)]
        
        investment = get_investment(self.user_id)
        inventory = investment.inventory

        if random.randint(1, 100) == 1:
            rewards = random.sample(self.CURRENCIES, k=random.randint(2, 3))
            amounts = {currency: random.randint(1, 5) for currency in rewards}
            for currency, amount in amounts.items():
                price = investment.current_prices.get(currency, self._currency_price(currency))
                inventory.add_item(currency, amount, price)
            
            final_balance = BalanceManager.load_balance(self.user_id)
            reward_text = " ".join(f"+{amount} {currency}" for currency, amount in amounts.items())
            return True, f"🍀 🍀 🍀 Супердроп! {reward_text} | Баланс: {final_balance}"

        counts = {symbol: reels.count(symbol) for symbol in self.SYMBOLS}
        reward = 0
        reward_currency = None
        for symbol, currency in zip(self.SYMBOLS, self.CURRENCIES):
            if counts[symbol] == 3:
                reward = 3
                reward_currency = currency
                break
            if counts[symbol] == 2:
                reward = 1
                reward_currency = currency
                break

        if reward_currency:
            price = investment.current_prices.get(reward_currency, self._currency_price(reward_currency))
            inventory.add_item(reward_currency, reward, price)
            result = f"🎉 +{reward} {reward_currency}!"
        elif len(set(reels)) == 3:
            result = "Пусто."
        else:
            result = "Мимо."

        final_balance = BalanceManager.load_balance(self.user_id)
        return True, f"{' '.join(reels)} {result} | Баланс: {final_balance}"

    @staticmethod
    def _currency_price(currency):
        return {"доллар": 450, "фунт стерлингов": 800, "биткоин": 1250}[currency]


# ---------- Инвестиции (валюты) ----------

class Investment:
    def __init__(self, user_id):
        self.user_id = user_id
        self.inventory = Inventory(user_id)
        self.currencies = {
            "рубль": {"price_range": (10, 100)},
            "юань": {"price_range": (100, 300)},
            "доллар": {"price_range": (300, 600)},
            "фунт стерлингов": {"price_range": (600, 1000)},
            "биткоин": {"price_range": (1000, 1500)},
        }
        self.current_prices = {}
        self.previous_prices = {}
        self.update_prices()

    def update_prices(self):
        new_prices = {}
        for currency, info in self.currencies.items():
            min_price, max_price = info["price_range"]
            if currency in self.current_prices:
                old_price = self.current_prices[currency]
                change_percent = random.randint(-15, 15)
                new_price = old_price + (old_price * change_percent // 100)
                new_price = max(min_price, min(max_price, new_price))
            else:
                new_price = random.randint(min_price, max_price)
            new_prices[currency] = new_price

        self.previous_prices = self.current_prices.copy()
        self.current_prices = new_prices
        self.inventory.update_item_prices(self.current_prices)

        return self.current_prices

    def get_prices_text(self):
        text = "📈 РЫНОК: \n"
        for currency in self.currencies:
            price = self.current_prices[currency]
            change = ""
            if currency in self.previous_prices:
                prev = self.previous_prices[currency]
                diff = price - prev
                percent = (diff * 100 // prev) if prev > 0 else 0
                arrow = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
                change = f" {arrow} {diff:+d} ({percent:+d}%)"
            text += f"{currency.upper()}: {price}{change}\n"
        return text

    def invest(self, currency_index, amount):
        currencies_list = list(self.currencies.keys())
        balance = BalanceManager.load_balance(self.user_id)

        if currency_index < 0 or currency_index >= len(currencies_list):
            return False, "Ошибка!", balance

        selected_currency = currencies_list[currency_index]
        currency_price = self.current_prices[selected_currency]
        total_cost = amount * currency_price

        if amount <= 0 or amount > 100:
            return False, "Неверное количество (1-100)!", balance

        if total_cost > balance:
            return False, f"Недостаточно средств!", balance

        balance -= total_cost
        BalanceManager.save_balance(self.user_id, balance)
        self.inventory.add_item(selected_currency, amount, currency_price)

        return True, f"✅ Куплено {amount} {selected_currency} за {total_cost} монет | Баланс: {balance}", balance


# ---------- Блэкджек ----------

class BlackjackGame:
    def __init__(self, user_id):
        self.user_id = user_id
        self.deck = []
        self.player_hand = []
        self.dealer_hand = []
        self.bet = 0
        self.game_active = False
        self.create_deck()

    def create_deck(self):
        suits = ["♥", "♦", "♣", "♠"]
        ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        self.deck = [(rank, suit) for suit in suits for rank in ranks] * 4
        random.shuffle(self.deck)

    def card_value(self, card):
        rank = card[0]
        if rank in ["J", "Q", "K"]:
            return 10
        elif rank == "A":
            return 11
        else:
            return int(rank)

    def hand_value(self, hand):
        value = sum(self.card_value(card) for card in hand)
        aces = sum(1 for card in hand if card[0] == "A")
        while value > 21 and aces > 0:
            value -= 10
            aces -= 1
        return value

    def get_game_text(self, show_dealer=False):
        text = "♠️ БЛЭКДЖЕК\n"

        if show_dealer:
            dealer_value = self.hand_value(self.dealer_hand)
            dealer_cards = " ".join([f"{r}{s}" for r, s in self.dealer_hand])
            text += f"Дилер: {dealer_cards} ({dealer_value})\n"
        else:
            first_card = f"{self.dealer_hand[0][0]}{self.dealer_hand[0][1]}"
            text += f"Дилер: {first_card} [?]\n"

        player_value = self.hand_value(self.player_hand)
        player_cards = " ".join([f"{r}{s}" for r, s in self.player_hand])
        text += f"Игрок: {player_cards} ({player_value})\n"

        if self.bet > 0:
            text += f"Ставка: {self.bet}\n"

        return text

    def start_game(self, bet):
        balance = BalanceManager.load_balance(self.user_id)

        if bet < MIN_BET:
            return False, f"Мин. ставка: {MIN_BET}!"
        if bet > balance:
            return False, "Недостаточно средств!"

        self.bet = bet
        self.game_active = True
        self.player_hand = []
        self.dealer_hand = []
        self.create_deck()

        for _ in range(2):
            self.player_hand.append(self.deck.pop())
            self.dealer_hand.append(self.deck.pop())

        balance -= bet
        BalanceManager.save_balance(self.user_id, balance)

        player_value = self.hand_value(self.player_hand)
        if player_value == 21:
            return self.end_game(True)

        return True, self.get_game_text()

    def hit(self):
        if not self.game_active:
            return False, "Игра окончена!"

        self.player_hand.append(self.deck.pop())
        player_value = self.hand_value(self.player_hand)

        if player_value > 21:
            return self.end_game(False)

        return True, self.get_game_text()

    def stand(self):
        if not self.game_active:
            return False, "Игра окончена!"

        while self.hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_value = self.hand_value(self.player_hand)
        dealer_value = self.hand_value(self.dealer_hand)

        if dealer_value > 21:
            return self.end_game(True)
        elif player_value > dealer_value:
            return self.end_game(True)
        elif player_value < dealer_value:
            return self.end_game(False)
        else:
            return self.end_game(None)

    def end_game(self, win):
        self.game_active = False
        balance = BalanceManager.load_balance(self.user_id)

        if win is None:
            balance += self.bet
            result_text = f"🤝 Ничья! Возврат."
        elif win:
            winnings = self.bet * 2
            balance += winnings
            result_text = f"🎉 Выигрыш: {winnings}!"
        else:
            result_text = f"😢 Проигрыш: {self.bet}."

        BalanceManager.save_balance(self.user_id, balance)

        result_text += f" Баланс: {balance}"

        game_text = self.get_game_text(show_dealer=True)
        return False, f"{game_text}\n{result_text}"


# ---------- Глобальное состояние ----------

active_investments = {}
blackjack_games = {}


def get_investment(user_id) -> Investment:
    if user_id not in active_investments:
        active_investments[user_id] = Investment(user_id)
    return active_investments[user_id]


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Лудка", callback_data="ludka")],
        [InlineKeyboardButton(text="📈 Инвестиции", callback_data="invest")],
        [InlineKeyboardButton(text="🏦 Кредиты", callback_data="loans")],
        [InlineKeyboardButton(text="🎒 Инвентарь", callback_data="inventory")],
        [InlineKeyboardButton(text="💰 Продажа", callback_data="sell")],
    ])


def back_keyboard(callback_data: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)],
    ])


def loans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Микрозайм (1k - 20k)", callback_data="loan_micro")],
        [InlineKeyboardButton(text="💳 Кредит (20k - 50k)", callback_data="loan_credit")],
        [InlineKeyboardButton(text="🏠 Ипотека (до 100k)", callback_data="loan_mortgage")],
        [InlineKeyboardButton(text="❌ Закрыть все кредиты", callback_data="loan_pay_all")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def ludka_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Казино", callback_data="casino")],
        [InlineKeyboardButton(text="♠️ Блэкджек", callback_data="blackjack")],
        [InlineKeyboardButton(text="🎰 Игровой автомат", callback_data="slots")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])


def casino_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ALL IN", callback_data="casino_all_in")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ludka")],
    ])


def casino_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Еще раз", callback_data="casino")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def slots_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Крутить (500)", callback_data="slots_spin")],
        [InlineKeyboardButton(text="⬅️ В лудку", callback_data="ludka")],
    ])


def blackjack_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Взять карту", callback_data="blackjack_hit")],
        [InlineKeyboardButton(text="✋ Остановиться", callback_data="blackjack_stand")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


async def safe_edit(query: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


def balance_line(balance: int) -> str:
    key_rate = KeyRateManager.get_rate()
    return f"💰 Баланс: {balance} монет | 🏛 Ставка ЦБ: {key_rate}%"


# ---------- Хэндлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    get_investment(user_id)

    balance = BalanceManager.load_balance(user_id)

    await message.answer(
        f"🎮 Меню | {balance_line(balance)}",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    user_id = query.from_user.id
    get_investment(user_id)

    balance = BalanceManager.load_balance(user_id)

    await safe_edit(
        query,
        f"🎮 Меню | {balance_line(balance)}",
        main_menu_keyboard(),
    )


# ---------- Раздел Кредиты ----------

@router.callback_query(F.data == "loans")
async def cb_loans(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    user_id = query.from_user.id
    loan_mgr = LoanManager(user_id)

    await safe_edit(query, loan_mgr.get_loans_text(), loans_keyboard())


@router.callback_query(F.data == "loan_micro")
async def cb_loan_micro(query: CallbackQuery, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    loan_mgr = LoanManager(user_id)

    if loan_mgr.get_loans_count() >= 3:
        await safe_edit(query, "🛑 У вас уже 3 активных кредита! Погасите имеющиеся, чтобы взять новый.", back_keyboard("loans"))
        return

    rate = round(KeyRateManager.get_rate() + 40.0, 1)  # Высокие %
    await state.set_state(Form.microloan_amount)

    await safe_edit(
        query,
        f"⚡ **Микрозайм**\nСумма: от 1,000 до 20,000 монет.\nТекущий процент: {rate}%\n\nВведите желаемую сумму:",
        back_keyboard("loans"),
    )


@router.callback_query(F.data == "loan_credit")
async def cb_loan_credit(query: CallbackQuery, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    loan_mgr = LoanManager(user_id)

    if loan_mgr.get_loans_count() >= 3:
        await safe_edit(query, "🛑 У вас уже 3 активных кредита! Погасите имеющиеся, чтобы взять новый.", back_keyboard("loans"))
        return

    rate = round(KeyRateManager.get_rate() + 15.0, 1)  # Средние %
    await state.set_state(Form.credit_amount)

    await safe_edit(
        query,
        f"💳 **Обычный кредит**\nСумма: от 20,000 до 50,000 монет.\nТекущий процент: {rate}%\n\nВведите желаемую сумму:",
        back_keyboard("loans"),
    )


@router.callback_query(F.data == "loan_mortgage")
async def cb_loan_mortgage(query: CallbackQuery, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    loan_mgr = LoanManager(user_id)

    if loan_mgr.get_loans_count() >= 3:
        await safe_edit(query, "🛑 У вас уже 3 активных кредита! Погасите имеющиеся, чтобы взять новый.", back_keyboard("loans"))
        return

    rate = round(KeyRateManager.get_rate() + 3.0, 1)  # Низкие %
    await state.set_state(Form.mortgage_amount)

    await safe_edit(
        query,
        f"🏠 **Ипотека**\nСумма: до 100,000 монет.\nТекущий процент: {rate}%\n\nВведите желаемую сумму:",
        back_keyboard("loans"),
    )


@router.callback_query(F.data == "loan_pay_all")
async def cb_loan_pay_all(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id
    loan_mgr = LoanManager(user_id)

    _, msg = loan_mgr.pay_all_loans()
    await safe_edit(query, msg, back_keyboard("loans"))


@router.callback_query(F.data == "ludka")
async def cb_ludka(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    await safe_edit(query, "🎲 ЛУДКА", ludka_keyboard())


@router.callback_query(F.data == "slots")
async def cb_slots(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    balance = BalanceManager.load_balance(query.from_user.id)
    await safe_edit(
        query,
        f"🎰 ИГРОВОЙ АВТОМАТ\nСтоимость: {SlotMachineGame.SPIN_COST}\nБаланс: {balance}",
        slots_keyboard(),
    )


@router.callback_query(F.data == "slots_spin")
async def cb_slots_spin(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id
    game = SlotMachineGame(user_id)
    balance = BalanceManager.load_balance(user_id)
    
    if balance < game.SPIN_COST:
        await safe_edit(query, f"Недостаточно монет! Стоимость: {game.SPIN_COST}", slots_keyboard())
        return

    for _ in range(18):
        frame = " ".join(random.choice(game.SYMBOLS) for _ in range(3))
        await safe_edit(query, f"🎰 {frame} 🎰", slots_keyboard())
        await asyncio.sleep(0.12)

    _, result = game.spin()
    await safe_edit(query, f"🎰 {result}", slots_keyboard())


@router.callback_query(F.data == "casino")
async def cb_casino(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(Form.casino_bet)
    await safe_edit(
        query,
        f"🎰 КАЗИНО\nВведите сумму ставки (мин. {MIN_BET}) или нажмите ALL IN:",
        casino_keyboard(),
    )


@router.callback_query(F.data == "casino_all_in")
async def cb_casino_all_in(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    user_id = query.from_user.id
    casino = CasinoGame(user_id)
    _, result_message, _ = casino.play_all_in()
    await safe_edit(query, result_message, casino_result_keyboard())


@router.callback_query(F.data == "blackjack")
async def cb_blackjack(query: CallbackQuery, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id

    if user_id in blackjack_games and blackjack_games[user_id].game_active:
        game = blackjack_games[user_id]
        await safe_edit(query, game.get_game_text(), blackjack_keyboard())
    else:
        await state.set_state(Form.blackjack_bet)
        await safe_edit(
            query,
            f"♠️ БЛЭКДЖЕК\nВведите сумму ставки (мин. {MIN_BET}):",
            back_keyboard(),
        )


@router.callback_query(F.data == "blackjack_hit")
async def cb_blackjack_hit(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id

    if user_id not in blackjack_games:
        await safe_edit(query, "Нет активной игры!", back_keyboard())
        return

    game = blackjack_games[user_id]
    success, message = game.hit()

    if success:
        await safe_edit(query, message, blackjack_keyboard())
    else:
        await safe_edit(query, message, back_keyboard())
        blackjack_games.pop(user_id, None)


@router.callback_query(F.data == "blackjack_stand")
async def cb_blackjack_stand(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id

    if user_id not in blackjack_games:
        await safe_edit(query, "Нет активной игры!", back_keyboard())
        return

    game = blackjack_games[user_id]
    _, message = game.stand()

    await safe_edit(query, message, back_keyboard())
    blackjack_games.pop(user_id, None)


@router.callback_query(F.data == "invest")
async def cb_invest(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    user_id = query.from_user.id
    investment = get_investment(user_id)

    balance = BalanceManager.load_balance(user_id)

    text = f"{investment.get_prices_text()}\n{balance_line(balance)}\nВыберите валюту:"

    keyboard = []
    currencies = list(investment.currencies.keys())
    for i, currency in enumerate(currencies):
        keyboard.append([InlineKeyboardButton(
            text=f"{currency.capitalize()} ({investment.current_prices[currency]})",
            callback_data=f"invest_{i}",
        )])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])

    await safe_edit(query, text, InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("invest_"))
async def cb_invest_currency(query: CallbackQuery, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    investment = get_investment(user_id)

    try:
        currency_index = int(query.data.split("_")[1])
        currencies = list(investment.currencies.keys())
        selected_currency = currencies[currency_index]
        price = investment.current_prices[selected_currency]
    except (ValueError, IndexError):
        await safe_edit(query, "Ошибка выбора!", back_keyboard("invest"))
        return

    await state.set_state(Form.invest_amount)
    await state.update_data(invest_currency=currency_index)

    await safe_edit(
        query,
        f"📊 {selected_currency.upper()} | Цена: {price}\nВведите количество:",
        back_keyboard("invest"),
    )


@router.callback_query(F.data == "inventory")
async def cb_inventory(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id
    investment = get_investment(user_id)
    await safe_edit(query, investment.inventory.get_inventory_text(), back_keyboard())


@router.callback_query(F.data == "sell")
async def cb_sell(query: CallbackQuery, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    investment = get_investment(user_id)
    inventory = investment.inventory
    items = inventory.load_inventory()

    if not items:
        await safe_edit(query, "📭 Ваш инвентарь пуст!", back_keyboard())
        return

    text = "💰 ПРОДАЖА:\n"
    for i, item in enumerate(items, 1):
        profit_loss = item["current_price"] - item["purchase_price"]
        arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"
        text += f"{i}. {item['name'].upper()} | {item['amount']} ед. | Куплено: {item['purchase_price']} | Текущая: {item['current_price']} {arrow}\n"

    text += "\nВведите номер и количество через пробел (например: 1 10) или нажмите кнопку «Продать всё»:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💥 Продать всё", callback_data="sell_all")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])

    await state.set_state(Form.sell_item)
    await safe_edit(query, text, keyboard)


@router.callback_query(F.data == "sell_all")
async def cb_sell_all(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    user_id = query.from_user.id
    investment = get_investment(user_id)
    inventory = investment.inventory

    total_sale, profit_loss = inventory.sell_all_items()

    if total_sale > 0:
        balance = BalanceManager.load_balance(user_id)
        new_balance = balance + total_sale
        BalanceManager.save_balance(user_id, new_balance)

        arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"
        result_message = (f"💥 Проданы все предметы!\n"
                          f"Получено: {total_sale} монет\n"
                          f"П/У: {arrow} {profit_loss:+d}\n"
                          f"Новый баланс: {new_balance}")
    else:
        result_message = "📭 Инвентарь пуст, нечего продавать!"

    await safe_edit(query, result_message, back_keyboard())


@router.callback_query(F.data == "balance")
async def cb_balance(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id
    balance = BalanceManager.load_balance(user_id)
    await safe_edit(query, balance_line(balance), back_keyboard())


# ---------- Обработка текстовых сообщений по состояниям ----------

@router.message(Form.microloan_amount)
async def msg_microloan_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = int((message.text or "").strip())
    except (ValueError, TypeError):
        await message.answer("Ошибка! Введите целое число.")
        return

    if amount < 1000 or amount > 20000:
        await message.answer("Ошибка! Сумма микрозайма должна быть от 1,000 до 20,000 монет.")
        return

    rate = round(KeyRateManager.get_rate() + 40.0, 1)
    loan_mgr = LoanManager(user_id)
    _, msg, _ = loan_mgr.take_loan("Микрозайм", amount, rate)

    await state.clear()
    await message.answer(msg, reply_markup=back_keyboard("loans"))


@router.message(Form.credit_amount)
async def msg_credit_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = int((message.text or "").strip())
    except (ValueError, TypeError):
        await message.answer("Ошибка! Введите целое число.")
        return

    if amount < 20000 or amount > 50000:
        await message.answer("Ошибка! Сумма обычного кредита должна быть от 20,000 до 50,000 монет.")
        return

    rate = round(KeyRateManager.get_rate() + 15.0, 1)
    loan_mgr = LoanManager(user_id)
    _, msg, _ = loan_mgr.take_loan("Кредит", amount, rate)

    await state.clear()
    await message.answer(msg, reply_markup=back_keyboard("loans"))


@router.message(Form.mortgage_amount)
async def msg_mortgage_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = int((message.text or "").strip())
    except (ValueError, TypeError):
        await message.answer("Ошибка! Введите целое число.")
        return

    if amount <= 0 or amount > 100000:
        await message.answer("Ошибка! Сумма ипотеки должна быть до 100,000 монет.")
        return

    rate = round(KeyRateManager.get_rate() + 3.0, 1)
    loan_mgr = LoanManager(user_id)
    _, msg, _ = loan_mgr.take_loan("Ипотека", amount, rate)

    await state.clear()
    await message.answer(msg, reply_markup=back_keyboard("loans"))


@router.message(Form.casino_bet)
async def msg_casino_bet(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        bet = int(float((message.text or "").strip()))
    except (ValueError, TypeError):
        await message.answer("Ошибка! Введите число.")
        return

    casino = CasinoGame(user_id)
    _, result_message, _ = casino.play_round(bet)
    await state.clear()

    await message.answer(result_message, reply_markup=casino_result_keyboard())


@router.message(Form.blackjack_bet)
async def msg_blackjack_bet(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        bet = int(float((message.text or "").strip()))
    except (ValueError, TypeError):
        await message.answer("Ошибка! Введите число.")
        return

    game = BlackjackGame(user_id)
    success, result_message = game.start_game(bet)

    if success:
        blackjack_games[user_id] = game
        await state.clear()
        await message.answer(result_message, reply_markup=blackjack_keyboard())
    else:
        await message.answer(result_message, reply_markup=back_keyboard())


@router.message(Form.invest_amount)
async def msg_invest_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = int((message.text or "").strip())
    except (ValueError, TypeError):
        await message.answer("Ошибка! Введите число.")
        return

    data = await state.get_data()
    currency_index = data.get("invest_currency", 0)

    investment = get_investment(user_id)
    _, result_message, _ = investment.invest(currency_index, amount)
    await state.clear()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Инвестировать еще", callback_data="invest")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])

    await message.answer(result_message, reply_markup=keyboard)


@router.message(Form.sell_item)
async def msg_sell_item(message: Message, state: FSMContext):
    user_id = message.from_user.id
    parts = (message.text or "").split()
    
    if len(parts) != 2:
        await message.answer("Ошибка! Введите номер и количество через пробел.")
        return

    try:
        item_index = int(parts[0]) - 1
        amount_to_sell = int(parts[1])
    except ValueError:
        await message.answer("Ошибка формата!")
        return

    investment = get_investment(user_id)
    inventory = investment.inventory
    item_name, total_sale, profit_loss, current_price = inventory.sell_item(item_index, amount_to_sell)

    if item_name:
        balance = BalanceManager.load_balance(user_id)
        new_balance = balance + total_sale
        BalanceManager.save_balance(user_id, new_balance)

        arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"
        
        result_message = (f"✅ Продано: {amount_to_sell} {item_name} | Получено: {total_sale}\n"
                          f"П/У: {arrow} {profit_loss:+d} | Баланс: {new_balance}")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💥 Продать всё", callback_data="sell_all")],
            [InlineKeyboardButton(text="💰 Продать еще", callback_data="sell")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ])

        await state.clear()
        await message.answer(result_message, reply_markup=keyboard)
    else:
        await message.answer("Ошибка продажи!")


@router.message()
async def msg_fallback(message: Message, state: FSMContext):
    user_id = message.from_user.id
    get_investment(user_id)
    current_balance = BalanceManager.load_balance(user_id)

    await message.answer(
        f"🎮 Меню | {balance_line(current_balance)}",
        reply_markup=main_menu_keyboard(),
    )


# ---------- Фоновое обновление цен и ключевой ставки ----------

async def update_prices_periodically():
    while True:
        await asyncio.sleep(30)
        KeyRateManager.update_rate()  # Обновление ставки ЦБ
        for user_id in list(active_investments.keys()):
            try:
                active_investments[user_id].update_prices()
            except Exception as e:
                print(f"Ошибка обновления цен для {user_id}: {e}")


# ---------- Запуск ----------

async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(update_prices_periodically())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
