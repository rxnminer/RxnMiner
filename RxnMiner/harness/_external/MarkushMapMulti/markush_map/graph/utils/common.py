from typing import List, Tuple


def get_nums_indices(nums: List[int]) -> List[Tuple[int, int]]:
    return sum([[(i, j) for j in range(num)] for i, num in enumerate(nums)], [])


if __name__ == '__main__':
    print(get_nums_indices([3]))
    print(get_nums_indices([4, 7, 0, 1]))
