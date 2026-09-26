

def find_common_sets(list1, list2):
    """
    Returns a list of sets that appear in both input lists.

    Args:
        list1: First list of sets
        list2: Second list of sets

    Returns:
        A list containing sets that exist in both list1 and list2
    """
    # Convert one list to a list of frozensets for hashability
    frozen_sets1 = [frozenset(s) for s in list1]

    # Create a dictionary to track sets by their frozenset representation
    result = []

    # Check each set in list2 if it exists in list1
    for s in list2:
        frozen_s = frozenset(s)
        if frozen_s in frozen_sets1:
            # Add the original set to results
            result.append(s)

    return result
