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
MIN_BET = 50
DEFAULT_BALANCE = 100

router = Router()


# ---------- Состояния FSM ----------

class Form(StatesGroup):
    casino_bet = State()
    blackjack_bet = State()
    invest_amount = State()
    sell_item = State()


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

    @staticmethod
    def check_and_grant_bonus(user_id):
        balance = BalanceManager.load_balance(user_id)
        inventory = Inventory(user_id)

        if balance < MIN_BET and not inventory.items:
            bonus = MIN_BET
            new_balance = balance + bonus
            BalanceManager.save_balance(user_id, new_balance)
            return True, bonus, new_balance

        return False, 0, balance


# ---------- Инвентарь ----------

class Inventory:
    def __init__(self, user_id):
        self.user_id = user_id
        self.items = self.load_inventory()

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

    def get_inventory_text(self):
        if not self.items:
            return "📭 Ваш инвентарь пуст!"

        text = "🎒 ВАШ ИНВЕНТАРЬ:\n\n"

        for i, item in enumerate(self.items, 1):
            current_value = item["amount"] * item["current_price"]
            purchase_value = item["amount"] * item["purchase_price"]

            profit_loss = current_value - purchase_value
            profit_percent = (profit_loss * 100 // purchase_value) if purchase_value > 0 else 0

            arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"

            text += f"{i}. {item['name'].upper()}\n"
            text += f"   Количество: {item['amount']} ед.\n"
            text += f"   Цена покупки: {item['purchase_price']} монет\n"
            text += f"   Текущая цена: {item['current_price']} монет\n"
            text += f"   Прибыль/убыток: {arrow} {profit_loss:+d} ({profit_percent:+d}%)\n"
            text += f"   Общая стоимость: {current_value} монет\n\n"

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
        for payout in self.payouts:
            cumulative_chance += payout["chance"]
            if chance <= cumulative_chance:
                if payout["multiplier"] == 0:
                    win = bet // 4
                else:
                    win = bet * payout["multiplier"]
                message = payout["message"]
                net_result = win - bet
                new_balance = balance + net_result
                BalanceManager.save_balance(self.user_id, new_balance)
                prefix = "ALL IN! " if all_in else ""
                return True, (
                    f"{prefix}{message}\nСтавка: {bet}\nВыигрыш: {win}\n"
                    f"Чистая прибыль: {'+' if net_result >= 0 else ''}{net_result}\n"
                    f"Текущий баланс: {new_balance}"
                ), new_balance

        win = bet // 4
        net_result = win - bet
        new_balance = balance + net_result
        BalanceManager.save_balance(self.user_id, new_balance)
        prefix = "ALL IN! " if all_in else ""
        return True, (
            f"{prefix}Лох\nСтавка: {bet}\nВыигрыш: {win}\n"
            f"Чистая прибыль: {'+' if net_result >= 0 else ''}{net_result}\n"
            f"Текущий баланс: {new_balance}"
        ), new_balance

    def play_all_in(self):
        """Ставит весь текущий баланс игрока."""
        balance = BalanceManager.load_balance(self.user_id)

        if balance <= 0:
            return False, "Баланс пуст — идти ва-банк не с чем!", balance

        if balance < MIN_BET:
            return False, f"Минимальная ставка - {MIN_BET} монет, а у вас всего {balance}!", balance

        return self.play_round(balance, all_in=True)


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
        text = "📈 ВАЛЮТНЫЙ РЫНОК:\n\n"
        for currency in self.currencies:
            price = self.current_prices[currency]
            change = ""
            if currency in self.previous_prices:
                prev = self.previous_prices[currency]
                diff = price - prev
                percent = (diff * 100 // prev) if prev > 0 else 0
                arrow = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
                change = f" {arrow} {diff:+d} ({percent:+d}%)"
            text += f"{currency.upper()}\n"
            text += f"Цена: {price} монет{change}\n\n"
        return text

    def invest(self, currency_index, amount):
        currencies_list = list(self.currencies.keys())
        balance = BalanceManager.load_balance(self.user_id)

        if currency_index < 0 or currency_index >= len(currencies_list):
            return False, "Неверный выбор валюты!", balance

        selected_currency = currencies_list[currency_index]
        currency_price = self.current_prices[selected_currency]
        total_cost = amount * currency_price

        if amount <= 0:
            return False, "Количество должно быть положительным!", balance
        if amount > 100:
            return False, "Количество не должно превышать 100!", balance

        if total_cost > balance:
            return False, f"Недостаточно средств! Нужно: {total_cost}, есть: {balance}", balance

        balance -= total_cost
        BalanceManager.save_balance(self.user_id, balance)
        self.inventory.add_item(selected_currency, amount, currency_price)

        return True, (
            f"✅ Куплено {amount} ед. {selected_currency} за {total_cost} монет\n"
            f"Текущая цена: {currency_price} монет\nБаланс: {balance}"
        ), balance


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
        text = "♠️ БЛЭКДЖЕК\n\n"

        if show_dealer:
            dealer_value = self.hand_value(self.dealer_hand)
            dealer_cards = " ".join([f"{r}{s}" for r, s in self.dealer_hand])
            text += f"Дилер: {dealer_cards}\n"
            text += f"Очки дилера: {dealer_value}\n\n"
        else:
            first_card = f"{self.dealer_hand[0][0]}{self.dealer_hand[0][1]}"
            text += f"Дилер: {first_card}, [скрытая]\n\n"

        player_value = self.hand_value(self.player_hand)
        player_cards = " ".join([f"{r}{s}" for r, s in self.player_hand])
        text += f"Твои карты: {player_cards}\n"
        text += f"Твои очки: {player_value}\n\n"

        if self.bet > 0:
            text += f"Ставка: {self.bet} монет\n"

        if not show_dealer and self.game_active:
            text += "\nВыберите действие:"

        return text

    def start_game(self, bet):
        balance = BalanceManager.load_balance(self.user_id)

        if bet < MIN_BET:
            return False, f"Минимальная ставка - {MIN_BET} монет!"

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
        self.player_hand.append(self.deck.pop())
        player_value = self.hand_value(self.player_hand)

        if player_value > 21:
            return self.end_game(False)

        return True, self.get_game_text()

    def stand(self):
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
            result_text = f"🤝 Ничья! Ставка возвращена.\nБаланс: {balance}"
        elif win:
            winnings = self.bet * 2
            balance += winnings
            result_text = f"🎉 Поздравляем! Вы выиграли {winnings} монет!\nБаланс: {balance}"
        else:
            result_text = f"😢 Вы проиграли {self.bet} монет.\nБаланс: {balance}"

        BalanceManager.save_balance(self.user_id, balance)

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
        [InlineKeyboardButton(text="🎰 Казино", callback_data="casino")],
        [InlineKeyboardButton(text="♠️ Блэкджек", callback_data="blackjack")],
        [InlineKeyboardButton(text="📈 Инвестировать", callback_data="invest")],
        [InlineKeyboardButton(text="🎒 Мой инвентарь", callback_data="inventory")],
        [InlineKeyboardButton(text="💰 Продать валюту", callback_data="sell")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="balance")],
    ])


def back_keyboard(callback_data: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)],
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


def balance_line(granted: bool, bonus_amount: int, balance: int) -> str:
    text = f"💰 Ваш баланс: {balance} монет"
    if granted:
        text += f"\n🎁 Вам начислен бонус {bonus_amount} монет (баланс < {MIN_BET} и пустой инвентарь)"
    return text


# ---------- Хэндлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    get_investment(user_id)

    granted, bonus_amount, new_balance = BalanceManager.check_and_grant_bonus(user_id)

    await message.answer(
        f"🎮 Казино (сварщикам вход воспрещен!)\n{balance_line(granted, bonus_amount, new_balance)}",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    user_id = query.from_user.id
    get_investment(user_id)

    granted, bonus_amount, new_balance = BalanceManager.check_and_grant_bonus(user_id)

    await safe_edit(
        query,
        f"🎮 Главное меню\n{balance_line(granted, bonus_amount, new_balance)}",
        main_menu_keyboard(),
    )


@router.callback_query(F.data == "casino")
async def cb_casino(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(Form.casino_bet)
    await safe_edit(
        query,
        f"🎰 КАЗИНО\nМинимальная ставка: {MIN_BET} монет\n\nВведите сумму ставки:",
        back_keyboard(),
    )


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
            f"♠️ БЛЭКДЖЕК\nМинимальная ставка: {MIN_BET} монет\n\nВведите сумму ставки:",
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

    granted, bonus_amount, new_balance = BalanceManager.check_and_grant_bonus(user_id)

    text = investment.get_prices_text()
    text += balance_line(granted, bonus_amount, new_balance) + "\n\n"
    text += "Выберите валюту для инвестиции:"

    keyboard = []
    currencies = list(investment.currencies.keys())
    for i, currency in enumerate(currencies):
        keyboard.append([InlineKeyboardButton(
            text=f"{i + 1}. {currency.capitalize()} - {investment.current_prices[currency]} монет",
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
        await safe_edit(query, "Ошибка выбора валюты!", back_keyboard("invest"))
        return

    await state.set_state(Form.invest_amount)
    await state.update_data(invest_currency=currency_index)

    await safe_edit(
        query,
        f"📊 {selected_currency.upper()}\n"
        f"Текущая цена: {price} монет\n\n"
        f"Введите количество для покупки:",
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

    if not inventory.items:
        await safe_edit(query, "📭 Ваш инвентарь пуст!", back_keyboard())
        return

    text = "💰 ПРОДАЖА ВАЛЮТЫ:\n\n"
    for i, item in enumerate(inventory.items, 1):
        profit_loss = item["current_price"] - item["purchase_price"]
        arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"
        text += f"{i}. {item['name'].upper()} - {item['amount']} ед.\n"
        text += f"   Куплено по: {item['purchase_price']} монет\n"
        text += f"   Текущая цена: {item['current_price']} монет {arrow}\n\n"

    text += "Введите номер позиции и количество через пробел (например: 1 10):"

    await state.set_state(Form.sell_item)
    await safe_edit(query, text, back_keyboard())


@router.callback_query(F.data == "balance")
async def cb_balance(query: CallbackQuery):
    await query.answer()
    user_id = query.from_user.id

    granted, bonus_amount, new_balance = BalanceManager.check_and_grant_bonus(user_id)

    await safe_edit(query, balance_line(granted, bonus_amount, new_balance), back_keyboard())


# ---------- Обработка текстовых сообщений по состояниям ----------

@router.message(Form.casino_bet)
async def msg_casino_bet(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        bet = int(float(message.text))
    except (ValueError, TypeError):
        await message.answer("Введите корректное число!")
        return

    casino = CasinoGame(user_id)
    _, result_message, _ = casino.play_round(bet)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Сыграть еще", callback_data="casino")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])

    await message.answer(result_message, reply_markup=keyboard)


@router.message(Form.blackjack_bet)
async def msg_blackjack_bet(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        bet = int(float(message.text))
    except (ValueError, TypeError):
        await message.answer("Введите корректное число!")
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
        amount = int(message.text)
    except (ValueError, TypeError):
        await message.answer("Введите корректное число!")
        return

    data = await state.get_data()
    currency_index = data.get("invest_currency", 0)

    investment = get_investment(user_id)
    _, result_message, _ = investment.invest(currency_index, amount)

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
        await message.answer("Введите номер и количество через пробел!")
        return

    try:
        item_index = int(parts[0]) - 1
        amount_to_sell = int(parts[1])
    except ValueError:
        await message.answer("Введите корректные числа!")
        return

    investment = get_investment(user_id)
    inventory = investment.inventory
    item_name, total_sale, profit_loss, current_price = inventory.sell_item(item_index, amount_to_sell)

    if item_name:
        balance = BalanceManager.load_balance(user_id)
        new_balance = balance + total_sale
        BalanceManager.save_balance(user_id, new_balance)

        arrow = "📈" if profit_loss > 0 else "📉" if profit_loss < 0 else "➡️"

        result_message = "✅ Продажа завершена!\n"
        result_message += f"Продано: {amount_to_sell} ед. {item_name}\n"
        result_message += f"Текущая цена: {current_price} монет\n"
        result_message += f"Получено: {total_sale} монет\n"
        result_message += f"Прибыль/убыток: {arrow} {profit_loss:+d} монет\n"
        result_message += f"Новый баланс: {new_balance} монет"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Продать еще", callback_data="sell")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ])

        await message.answer(result_message, reply_markup=keyboard)
    else:
        await message.answer("Ошибка продажи! Проверьте номер и количество.")


@router.message()
async def msg_fallback(message: Message, state: FSMContext):
    user_id = message.from_user.id
    get_investment(user_id)

    granted, bonus_amount, current_balance = BalanceManager.check_and_grant_bonus(user_id)

    if granted:
        await message.answer(
            f"🎁 Вам начислен бонус {bonus_amount} монет (баланс < {MIN_BET} и пустой инвентарь)\n"
            f"💰 Новый баланс: {current_balance} монет"
        )

    await message.answer(
        f"🎮 Казино (сварщикам вход воспрещен!)\n{balance_line(False, 0, current_balance)}",
        reply_markup=main_menu_keyboard(),
    )


# ---------- Фоновое обновление цен ----------

async def update_prices_periodically():
    while True:
        await asyncio.sleep(30)
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
