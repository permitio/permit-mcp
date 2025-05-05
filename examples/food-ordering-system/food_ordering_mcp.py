from typing import List, Tuple
import aiosqlite
import sqlite3
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import os
import sys
from permit_mcp import PermitServer
from mcp.server.fastmcp.exceptions import ToolError
from permit_client import permit

load_dotenv()

if len(sys.argv) > 1:
    DB_NAME = sys.argv[1]
else:
    DB_NAME = "test.db"

TENANT = os.getenv("TENANT")

conn = sqlite3.connect(DB_NAME)
cursor = conn.cursor()

# Initialize FastMCP instance and
# the Permit MCP server  to make it's tools available.
mcp = FastMCP("family_food_ordering_system")
permit_server = PermitServer(mcp)


@mcp.tool()
async def list_dishes(user_id: str, restaurant_id: str) -> List[Tuple[str, float]]:
    """
    Lists the dishes available at a given restaurant along with their prices in dollars.
    Dishes are only listed when the user has access; otherwise and access request will need to be sent.

    Args:
        user_id: The ID of the user.
        restaurant_id: The key of the restaurant.
    """

    # Check if user is permitted in the restaurant
    permitted = await permit.check(user_id, 'read', f"restaurants:{restaurant_id}")
    if not permitted:
        raise ToolError(
            "Access denied. You are not permitted to view dishes from this restaurant."
        )

    async with aiosqlite.connect(DB_NAME) as db:
        # Fetch dishes
        dishes_query = """
            SELECT name, price FROM dishes
            WHERE restaurant_id = ?
        """
        cursor = await db.execute(dishes_query, (restaurant_id,))
        dishes = await cursor.fetchall()
        await cursor.close()
        return dishes


@mcp.tool()
async def order_dish(user_id: str, restaurant_id: str, dish_name: str) -> str:
    """
    Processes an order for a dish.

    Args:
        user_id: The ID of the person ordering.
        restaurant_id: The key of the restaurant.
        dish_name: The name of the dish to order.
    """
    MAX_ALLOWED_DISH_PRICE = 10  # 10 dollars

    async with aiosqlite.connect(DB_NAME) as db:
        # Get dish price
        dish_cursor = await db.execute(
            "SELECT price FROM dishes WHERE name = ? AND restaurant_id = ?",
            (dish_name, restaurant_id),
        )
        dish = await dish_cursor.fetchone()
        await dish_cursor.close()

        if dish is None:
            raise ToolError(
                f"Dish '{dish_name}' not found."
            )

        # Get user role
        user_cursor = await db.execute(
            "SELECT role FROM users WHERE id = ?",
            (user_id,),
        )
        user = await user_cursor.fetchone()
        await user_cursor.close()

    if user is None:
        raise ToolError(
            f"User with ID '{user_id}' not found. Please check the user ID."
        )

    # Check if user is permitted in the restaurant that serves this dish.
    permitted = await permit.check(user_id, "read", f"restaurants:{restaurant_id}")

    if not permitted:
        raise ToolError(
            "Access denied. You are not permitted to order from this restaurant."
        )

    # Check if user is permitted to order costly dishes.
    permitted = await permit.check(user_id, "operate", f"restaurants:{restaurant_id}")

    # Apply price restriction for children
    if user[0] == "child" and dish[0] > MAX_ALLOWED_DISH_PRICE and not permitted:
        raise ToolError(
            f"This dish costs ${dish[0]:.2f}, and you can only order dishes less than "
            f"${MAX_ALLOWED_DISH_PRICE:.2f}. To order this dish, you need to request an approval."
        )

    if permitted:
        await permit.api.users.unassign_role({
            "user": user_id,
            "role": "_Approved_",
            "resource_instance": f"restaurants:{restaurant_id}",
            "tenant": TENANT
        })

    return f"Order successfully placed for {dish_name}!"


if __name__ == "__main__":
    mcp.run(transport="stdio")
