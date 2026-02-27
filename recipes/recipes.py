import json
import os

RECIPES_FILE = os.path.join(os.path.dirname(__file__), 'recipes.json')

DEFAULT_RECIPES = {
}

def load_recipes():
    try:
        if os.path.exists(RECIPES_FILE):
            with open(RECIPES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            save_recipes(DEFAULT_RECIPES)
            return DEFAULT_RECIPES.copy()
    except Exception as e:
        print(f"Ошибка загрузки рецептов: {e}")
        return DEFAULT_RECIPES.copy()

def save_recipes(recipes_dict):
    try:
        with open(RECIPES_FILE, 'w', encoding='utf-8') as f:
            json.dump(recipes_dict, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Ошибка сохранения рецептов: {e}")
        return False

recipes = load_recipes()