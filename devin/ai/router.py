def select_model(task, complexity="normal"):

    if task == "autocomplete":
        return "local_small"

    if task == "coding":
        return "local_medium"

    if task == "architecture":
        return "cloud"

    if complexity == "high":
        return "cloud"

    return "local_medium"