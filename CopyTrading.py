import MetaTrader5 as mt5
import time
import os
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

FILE_PATH = "accounts.json"
DEFAULT_DATA = {
    "accounts": [
        {
            "login": 0,
            "password": "password",
            "server": "server",
            "nickName": "Master"
        },
        {
            "login": 0,
            "password": "password",
            "server": "server",
            "nickName": "Slave"
        }
    ]
}

if not os.path.exists(FILE_PATH):
    with open(FILE_PATH, 'w') as json_file:
        json.dump(DEFAULT_DATA, json_file, indent=4)
    logging.error(f"{FILE_PATH} created. Please enter the accounts' credentials!")
    exit()

with open(FILE_PATH, 'r') as json_file:
    accounts_data = json.load(json_file)["accounts"]

MASTER_ACCOUNT = accounts_data[0]
SLAVE_ACCOUNT = accounts_data[1]

def login_to_account(account):
    if not mt5.initialize():
        logging.error(f"Failed to initialize MT5: {mt5.last_error()}")
        return False
    if not mt5.login(account['login'], account['password'], account['server']):
        logging.error(f"Failed to login to account {account['login']}: {mt5.last_error()}")
        return False
    logging.info(f"Logged into account {account['login']}. Please activate Algo Trading in MetaTrader 5.")

    # Prompt user for confirmation
    input("Press Enter once Algo Trading is activated in the MetaTrader 5 terminal...")
    
    # Log confirmation and next step
    logging.info("Algo Trading activated. Waiting for trades...")
    return True


def monitor_trades():
    if not login_to_account(MASTER_ACCOUNT):
        return
    monitored_trades = {trade.ticket for trade in mt5.positions_get() or []}
    copied_trades = {}

    while True:
        current_trades = mt5.positions_get() or []
        current_tickets = {trade.ticket for trade in current_trades}

        # Detect new trades
        new_tickets = current_tickets - monitored_trades
        for ticket in new_tickets:
            trade = next(t for t in current_trades if t.ticket == ticket)
            logging.info(f"New trade detected: {trade}")
            if not login_to_account(SLAVE_ACCOUNT):
                return
            copy_trade(trade)
            if not login_to_account(MASTER_ACCOUNT):
                return
            copied_trades[ticket] = trade

        # Detect closed trades
        closed_tickets = monitored_trades - current_tickets
        for ticket in closed_tickets:
            if ticket in copied_trades:
                if not login_to_account(SLAVE_ACCOUNT):
                    return
                close_trade(copied_trades[ticket])
                del copied_trades[ticket]
                if not login_to_account(MASTER_ACCOUNT):
                    return

        monitored_trades = current_tickets
        time.sleep(1)

def copy_trade(trade):
    try:
        symbol = trade.symbol

        # Ensure the symbol is selected on the slave account
        if not mt5.symbol_select(symbol, True):
            logging.error(f"Symbol {symbol} is not tradable or not selected.")
            return None

        # Get symbol info
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            logging.error(f"Failed to get symbol info for {symbol}.")
            return None

        # Get master and slave balances
        master_balance = mt5.account_info().balance
        slave_balance = mt5.account_info().balance

        # Calculate proportional lot size
        proportional_volume = trade.volume * (slave_balance / master_balance)

        # Adjust volume to match symbol's constraints
        adjusted_volume = max(
            symbol_info.volume_min,
            round(proportional_volume / symbol_info.volume_step) * symbol_info.volume_step
        )
        if adjusted_volume < symbol_info.volume_min or adjusted_volume > symbol_info.volume_max:
            logging.error(
                f"Adjusted volume {adjusted_volume} is invalid for {symbol}. "
                f"Min: {symbol_info.volume_min}, Max: {symbol_info.volume_max}."
            )
            return None

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logging.error(f"Failed to get tick info for symbol {symbol}.")
            return None
        price = tick.ask if trade.type == mt5.ORDER_TYPE_BUY else tick.bid

        # Build trade request
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': adjusted_volume,
            'type': mt5.ORDER_TYPE_BUY if trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_SELL,
            'price': price,
            'deviation': 20,
            'magic': 0,
            'comment': 'Copied trade',
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': mt5.ORDER_FILLING_IOC,
        }

        # Log the trade request
        logging.info(f"Sending trade request: {request}")

        # Send trade request
        result = mt5.order_send(request)

        # Validate the result
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"Trade copied successfully: {result.order}")
            return result.order
        else:
            logging.error(f"Failed to copy trade. Retcode: {result.retcode}, Result: {result}")
            return None

    except Exception as e:
        logging.error(f"Error copying trade: {e}")
        return None

def close_trade(trade):
    try:
        symbol = trade.symbol
        close_type = mt5.ORDER_TYPE_SELL if trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

        # Ensure the symbol is selected
        if not mt5.symbol_select(symbol, True):
            logging.error(f"Symbol {symbol} is not tradable or not selected.")
            return False

        # Retrieve all positions on the slave account for this symbol
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            logging.error(f"No positions found for symbol {symbol} on the slave account.")
            return False

        # Use the first matching position (on the slave account)
        position_to_close = positions[0]  # Assuming one position per symbol in this example
        volume_to_close = position_to_close.volume

        # Get the current bid/ask price for closing
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logging.error(f"Failed to get tick info for symbol {symbol}.")
            return False
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        # Build the trade request
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'position': position_to_close.ticket,
            'symbol': symbol,
            'volume': volume_to_close,  # Use the slave account's position volume
            'type': close_type,
            'price': price,
            'deviation': 20,  # Allowable deviation in points
            'magic': 0,
            'comment': 'Close copied trade',
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': mt5.ORDER_FILLING_IOC,  # Adjust if needed
        }

        # Log the trade request
        logging.info(f"Sending close trade request: {request}")

        # Send the trade request
        result = mt5.order_send(request)

        # Validate the result
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"Trade closed successfully: {result.order}")
            return True
        else:
            logging.error(f"Failed to close trade. Retcode: {result.retcode}, Result: {result}")
            return False

    except Exception as e:
        logging.error(f"Error closing trade: {e}")
        return False



if __name__ == "__main__":
    try:
        monitor_trades()
    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    finally:
        mt5.shutdown()
