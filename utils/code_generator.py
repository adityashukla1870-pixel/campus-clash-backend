import random
import string

def generate_payment_code():

    characters = string.ascii_uppercase + string.digits

    code = "CC" + "".join(random.choices(characters, k=6))

    return code
