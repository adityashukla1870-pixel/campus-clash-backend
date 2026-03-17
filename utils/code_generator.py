import random
import string

def generate_payment_code():

    characters = string.ascii_uppercase + string.digits

    return "CC" + "".join(random.choices(characters, k=6))
