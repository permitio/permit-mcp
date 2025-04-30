from permit_client import permit
from dotenv import load_dotenv
import os
import bcrypt
import sqlite3
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from fastapi import WebSocket

load_dotenv()

DB_NAME = os.getenv("DB_NAME")
TENANT = os.getenv("TENANT")
SECRET_KEY = "your-secret-key"
ALGORITHM = "HS256"


async def init_db():
    print('Initializing database')

    # Connect to the database
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Create tables for users, restaurants, and dishes
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        role TEXT,
        hashed_password TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS restaurants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        allowed_for_children BOOLEAN
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS dishes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER,
        name TEXT,
        price REAL,
        FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
    )
    """)

    # Check if restaurants table is empty
    cursor.execute('SELECT COUNT(*) FROM restaurants')
    row = cursor.fetchone()

    if row[0] == 0:
        # Populate the users table with hashed passwords
        users_data = [
            ("joe", "parent", hash_password('joe_password')),
            ("jane", "parent", hash_password('jane_password')),
            ("henry", "child", hash_password('henry_password')),
            ("rose", "child", hash_password('rose_password')),
        ]
        cursor.executemany(
            "INSERT OR IGNORE INTO users (username, role, hashed_password) VALUES (?, ?, ?)",
            users_data
        )

        # Populate the restaurants table
        restaurants_data = [
            ("Pizza Palace", True),
            ("Burger Bonanza", True),
            ("Fancy French", False),
            ("Sushi World", False),
        ]
        cursor.executemany(
            "INSERT OR IGNORE INTO restaurants (name, allowed_for_children) VALUES (?, ?)",
            restaurants_data
        )

        # Retrieve the restaurants with their IDs
        cursor.execute(
            'SELECT id, name, allowed_for_children FROM restaurants')
        restaurants = cursor.fetchall()

        # Populate the dishes table based on restaurant names
        dishes_data = []
        for restaurant_id, restaurant_name, allowed in restaurants:
            if restaurant_name == "Pizza Palace":
                dishes_data.extend([
                    (restaurant_id, "Cheese Pizza", 8.99),
                    (restaurant_id, "Pepperoni Pizza", 10.99),
                    (restaurant_id, "Veggie Pizza", 9.49),
                ])
            elif restaurant_name == "Burger Bonanza":
                dishes_data.extend([
                    (restaurant_id, "Classic Burger", 7.99),
                    (restaurant_id, "Deluxe Burger", 12.99),
                    (restaurant_id, "Fries", 3.49),
                ])
            elif restaurant_name == "Fancy French":
                dishes_data.extend([
                    (restaurant_id, "Escargot", 15.99),
                    (restaurant_id, "Foie Gras", 19.99),
                    (restaurant_id, "Truffle Pasta", 18.49),
                ])
            elif restaurant_name == "Sushi World":
                dishes_data.extend([
                    (restaurant_id, "California Roll", 6.99),
                    (restaurant_id, "Sushi Platter", 22.99),
                    (restaurant_id, "Tempura", 9.99),
                ])

        cursor.executemany(
            "INSERT OR IGNORE INTO dishes (restaurant_id, name, price) VALUES (?, ?, ?)",
            dishes_data
        )

        print('Setting up Permit...')

        # Create Permit.io resource instances for each restaurant
        for restaurant_id, restaurant_name, allowed_for_children in restaurants:
            await permit.api.resource_instances.create({
                "resource": "restaurants",
                "key": restaurant_id,
                "tenant": TENANT,
                "attributes": {
                    "name": restaurant_name,
                    "allowed_for_children": bool(allowed_for_children)
                }
            })

        # Retrieve the users to synchronize with Permit.io
        cursor.execute("SELECT id, username, role FROM users")
        users = cursor.fetchall()

        # Sync users with Permit.io
        for user_id, username, role in users:
            await permit.api.sync_user({
                "key": user_id,
                "first_name": username
            })

        # Separate restaurants allowed for children
        children_restaurants = [r for r in restaurants if r[2]]

        # Assign roles using Permit.io based on user type
        for user_id, username, role in users:
            if role == "parent":
                parent_assignments = [
                    {
                        "user": user_id,
                        "role": "parent",
                        "tenant": TENANT,
                        "resource_instance": f"restaurants:{r[0]}",
                    }
                    for r in restaurants
                ]
                await permit.api.role_assignments.bulk_assign(parent_assignments)

                reviewer_assignments = [
                    {
                        "user": user_id,
                        "role": "_Reviewer_",
                        "tenant": TENANT,
                        "resource_instance": f"restaurants:{r[0]}",
                    }
                    for r in restaurants
                ]
                await permit.api.role_assignments.bulk_assign(reviewer_assignments)

            elif role == "child":
                child_assignments = [
                    {
                        "user": user_id,
                        "role": "child-can-view",
                        "tenant": TENANT,
                        "resource_instance": f"restaurants:{r[0]}",
                    }
                    for r in children_restaurants
                ]
                await permit.api.role_assignments.bulk_assign(child_assignments)

    # Commit changes and close the database connection
    conn.commit()
    conn.close()

    print('Database initialization complete.')
    return True


def filter_tools_by_role(tools, role):
    """
    Filter tools based on user role.
    """
    CHILD_ALLOWED_TOOLS = [
        "list_resource_instances",
        "create_operation_approval",
        "create_access_request",
        "list_dishes",
        'order_dish'
    ]

    if role == "parent":
        return tools

    if role == "child":
        return [tool for tool in tools if tool.name in CHILD_ALLOWED_TOOLS]

    return []


def convert_mcp_tools_to_gemini(mcp_tools):
    """
    Convert MCP tools to Gemini function declarations.
    """
    if not isinstance(mcp_tools, list):
        raise ValueError("Input must be an array of tool definitions.")

    gemini_function_declarations = []
    for index, mcp_tool in enumerate(mcp_tools):

        name = mcp_tool.name
        description = mcp_tool.description
        input_schema = mcp_tool.inputSchema

        if not isinstance(name, str) or name.strip() == "":
            raise ValueError(
                f"Tool definition at index {index} is missing the required 'name' key or name is empty: {mcp_tool}"
            )

        gemini_func_decl = {
            "name": name,
            "description": description if isinstance(description, str) else f"Executes the {name} tool."
        }

        if input_schema is not None:
            if not isinstance(input_schema, dict):
                raise ValueError(
                    f"Tool '{name}' at index {index} has an 'input_schema' that is not a plain object: {input_schema}"
                )

            # Deep clone the input schema to avoid modifying the original object
            import copy
            parameters_schema = copy.deepcopy(input_schema)

            if parameters_schema.get('properties') and isinstance(parameters_schema['properties'], dict):
                for param_name, param_schema in parameters_schema['properties'].items():
                    if param_schema and isinstance(param_schema, dict):
                        if 'default' in param_schema:
                            # Remove default property
                            del param_schema['default']

                            # Enhance description if needed
                            if isinstance(param_schema.get('description'), str) and 'optional' not in param_schema['description'].lower():
                                param_schema['description'] = f"Optional. {param_schema['description']}"
                            elif not param_schema.get('description'):
                                param_schema['description'] = f"Optional parameter {param_name}."

                        if 'anyOf' in param_schema:
                            # Check for string type in anyOf
                            if any(f.get('type') == 'string' for f in param_schema['anyOf']):
                                del param_schema['anyOf']
                                param_schema['type'] = 'string'
                            # Check for integer type in anyOf
                            elif any(f.get('type') == 'integer' for f in param_schema['anyOf']):
                                del param_schema['anyOf']
                                param_schema['type'] = 'integer'

            gemini_func_decl['parameters'] = parameters_schema

        gemini_function_declarations.append(gemini_func_decl)

    return gemini_function_declarations


def hash_password(password):
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password=pwd_bytes, salt=salt)
    return hashed_password


def verify_password(plain_password, hashed_password):
    password_byte_enc = plain_password.encode('utf-8')
    return bcrypt.checkpw(password=password_byte_enc, hashed_password=hashed_password)


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def get_user(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user_record = cursor.fetchone()

    conn.close()

    if user_record:
        return dict(user_record)
    return None


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta if expires_delta else timedelta(minutes=15))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_token_from_websocket(websocket: WebSocket) -> Optional[str]:
    auth_header = websocket.headers.get("authorization")
    if not auth_header:
        return None

    try:
        scheme, token = auth_header.split()
        if scheme.lower() != "bearer":
            return None
        return token
    except ValueError:
        return None


async def get_current_websocket_user(websocket: WebSocket) -> Optional[Dict]:
    token = await get_token_from_websocket(websocket)
    if not token:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None

        # Your existing function to get user data
        user = get_user(username=username)
        if user is None:
            return None

        return user
    except JWTError:
        return None


async def retry_tool_call(session: Any, name: str, args: Dict[str, Any]) -> Any:
    """
    Retries calling an MCP tool with exponential backoff.
    """
    MAX_RETRIES = 2  # Define the maximum number of retries

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = await session.call_tool(name, args)
            if result.isError:
                print(result)
                last_error = result
            else:
                return result
        except Exception as error:
            print(error, "This is the MCP tools call error")
            last_error = error

    return last_error
