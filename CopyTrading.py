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

def get_user_defined_lot_size(symbol_info):
    """
    Ask the user to input a fixed lot size for the slave account. Validate the input.
    """
    print(f"Symbol: {symbol_info.name}")
    print(f"Allowed lot size range: {symbol_info.volume_min} - {symbol_info.volume_max}")
    print(f"Lot size step: {symbol_info.volume_step}")

    while True:
        try:
            # Ask the user for the lot size
            user_lot_size = float(input("Enter the desired fixed lot size for the slave account (e.g., 0.1): "))
            
            # Validate the lot size
            if user_lot_size < symbol_info.volume_min or user_lot_size > symbol_info.volume_max:
                print(f"Invalid lot size. Must be between {symbol_info.volume_min} and {symbol_info.volume_max}.")
                continue

            # Ensure the lot size is a valid multiple of the volume step
            rounded_lot_size = round(user_lot_size / symbol_info.volume_step) * symbol_info.volume_step
            if abs(user_lot_size - rounded_lot_size) > 1e-6:
                print(f"Invalid lot size. Must be a multiple of the step size {symbol_info.volume_step}.")
                continue

            return rounded_lot_size

        except ValueError:
            print("Invalid input. Please enter a numeric value.")



def monitor_trades(fixed_lot_size):
    if not login_to_account(MASTER_ACCOUNT):
        return

    # Initialize monitored_trades as a dictionary with trade tickets as keys
    monitored_trades = {trade.ticket: trade for trade in mt5.positions_get() or []}
    copied_trades = {}

    while True:
        # Fetch the current trades from the master account
        current_trades = mt5.positions_get() or []
        current_trades_dict = {trade.ticket: trade for trade in current_trades}

        # Detect new trades
        new_tickets = set(current_trades_dict.keys()) - set(monitored_trades.keys())
        for ticket in new_tickets:
            trade = current_trades_dict[ticket]
            logging.info(f"New trade detected: {trade}")
            if not login_to_account(SLAVE_ACCOUNT):
                return
            copy_trade(trade, fixed_lot_size)
            if not login_to_account(MASTER_ACCOUNT):
                return
            copied_trades[ticket] = trade

        # Detect closed trades
        closed_tickets = set(monitored_trades.keys()) - set(current_trades_dict.keys())
        for ticket in closed_tickets:
            if ticket in copied_trades:
                if not login_to_account(SLAVE_ACCOUNT):
                    return
                close_trade(copied_trades[ticket])
                del copied_trades[ticket]
                if not login_to_account(MASTER_ACCOUNT):
                    return

        # Detect modified trades (SL/TP changes)
        for ticket, trade in current_trades_dict.items():
            if ticket in monitored_trades:
                prev_trade = monitored_trades[ticket]
                # Check if SL or TP values have changed
                if trade.sl != prev_trade.sl or trade.tp != prev_trade.tp:
                    logging.info(f"Trade modified (SL/TP changed): {trade}")
                    if not login_to_account(SLAVE_ACCOUNT):
                        return
                    update_position_on_slave(trade)
                    if not login_to_account(MASTER_ACCOUNT):
                        return

        # Update monitored_trades with the current trades
        monitored_trades = current_trades_dict
        time.sleep(1)

def copy_trade(trade, fixed_lot_size):
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
        adjusted_volume = min(adjusted_volume, symbol_info.volume_max)

        # Check if there is enough free margin for the adjusted volume
        slave_account_info = mt5.account_info()
        if not slave_account_info:
            logging.error(f"Failed to get slave account info: {mt5.last_error()}")
            return None

        # Calculate margin requirement for the adjusted volume
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logging.error(f"Failed to get tick info for symbol {symbol}.")
            return None
        price = tick.ask if trade.type == mt5.ORDER_TYPE_BUY else tick.bid

        margin_required = price * adjusted_volume * symbol_info.margin_initial
        if margin_required > slave_account_info.margin_free:
            logging.error(f"Not enough margin for trade. Required: {margin_required:.2f}, Free: {slave_account_info.margin_free:.2f}")
            # Reduce volume to fit within available margin
            adjusted_volume = max(
                symbol_info.volume_min,
                round((slave_account_info.margin_free / (price * symbol_info.margin_initial)) / symbol_info.volume_step)
                * symbol_info.volume_step
            )
            if adjusted_volume < symbol_info.volume_min:
                logging.error("Cannot open even the minimum volume due to insufficient margin.")
                return None

        # Build trade request
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': fixed_lot_size,
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

def update_position_on_slave(master_trade):
    """
    Update the corresponding position on the slave account with the same SL and TP as the master trade.
    """
    try:
        # Fetch all positions for the symbol on the slave account
        slave_positions = mt5.positions_get(symbol=master_trade.symbol)
        if not slave_positions:
            logging.error(f"No positions found on slave account for symbol {master_trade.symbol}.")
            return

        # Match the position by symbol and the most recent open time
        slave_position = sorted(
            slave_positions,
            key=lambda pos: pos.time,  # Sort by open time to find the most recent trade
            reverse=True
        )[0]  # Take the most recent position

        if not slave_position:
            logging.error(f"No matching position found on slave account for symbol {master_trade.symbol}.")
            return

        # Check if SL/TP updates are necessary
        if slave_position.sl == master_trade.sl and slave_position.tp == master_trade.tp:
            logging.info(f"Slave position already matches SL/TP of master position. No update required.")
            return

        # Build the trade request to modify SL and TP
        request = {
            'action': mt5.TRADE_ACTION_SLTP,
            'symbol': master_trade.symbol,
            'position': slave_position.ticket,
            'sl': master_trade.sl,
            'tp': master_trade.tp,
        }

        # Log the update request
        logging.info(f"Sending SL/TP update request: {request}")

        # Send the modification request
        result = mt5.order_send(request)

        # Validate the result
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"Position updated successfully on slave account: {result}")
        else:
            logging.error(f"Failed to update position on slave account. Retcode: {result.retcode}, Result: {result}")

    except Exception as e:
        logging.error(f"Error while updating position on slave account: {e}")

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
        # Log in to the slave account to get symbol info
        if not login_to_account(SLAVE_ACCOUNT):
            exit()
        symbol = "NAS100"  # Replace with the primary symbol you'll trade
        if not mt5.symbol_select(symbol, True):
            logging.error(f"Symbol {symbol} is not tradable or not selected.")
            exit()
        symbol_info = mt5.symbol_info(symbol)

        # Ask the user for the fixed lot size
        fixed_lot_size = get_user_defined_lot_size(symbol_info)

        # Start monitoring trades with the fixed lot size
        monitor_trades(fixed_lot_size)

    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    finally:
        mt5.shutdown()

