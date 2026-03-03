import random

def generate_payment_code(tournament_id):
    return f"CC{str(tournament_id)[-4:]}{random.randint(1000,9999)}"
