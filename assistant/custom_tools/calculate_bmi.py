
def execute(context, weight_kg: float, height_m: float) -> dict:
    if height_m <= 0:
        return {"status": "failure", "error": "Height must be greater than zero"}
    bmi = round(weight_kg / (height_m ** 2), 2)
    if bmi < 18.5:
        category = "underweight"
    elif bmi < 25.0:
        category = "normal"
    elif bmi < 30.0:
        category = "overweight"
    else:
        category = "obese"
    return {"status": "success", "bmi": bmi, "category": category}
