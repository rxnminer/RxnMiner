from typing import List, TypeVar, Dict, Tuple

T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")


def indent(s: str) -> str:
    return "\n".join([f"\t{ss}" for ss in s.split("\n")])


def sort_and_group(objects: List[T], reverse=False, sort_by_size=False) -> List[List[T]]:
    if not objects:
        return []

    # 首先对输入的对象列表进行排序
    sorted_objects = sorted(objects, reverse=reverse)

    # 初始化结果列表和当前分组
    result = []
    current_group = [sorted_objects[0]]

    # 遍历排序后的列表，从第二个元素开始
    for i in range(1, len(sorted_objects)):
        current_obj = sorted_objects[i]
        prev_obj = sorted_objects[i - 1]

        # 如果当前对象等于前一个对象，将其添加到当前分组
        if current_obj == prev_obj:
            current_group.append(current_obj)
        # 否则，完成当前分组并开始一个新的分组
        else:
            result.append(current_group)
            current_group = [current_obj]

    # 添加最后一个分组到结果中
    result.append(current_group)

    if sort_by_size:
        result = sorted(result, key=lambda r: len(r), reverse=True)
    return result


def pairing_dicts_values(d1: Dict[T1, T2], d2: Dict[T1, T2]) -> List[Tuple[T1, T1]]:
    # 创建一个值到键的映射，用于d2
    value_to_keys_d2 = {}
    for k, v in d2.items():
        value_to_keys_d2.setdefault(v, []).append(k)

    result = []
    used_keys_d2 = set()  # 用于跟踪已使用的d2中的键

    # 遍历d1中的每个键值对
    for k1, v1 in d1.items():
        # 如果d1中的值在d2中存在，且还有未使用的键
        if v1 in value_to_keys_d2 and value_to_keys_d2[v1]:
            # 获取d2中一个未使用的键
            for k2 in value_to_keys_d2[v1]:
                if k2 not in used_keys_d2:
                    result.append((k1, k2))
                    used_keys_d2.add(k2)
                    break

    return result


def check_lists_contain(l1: List[T], l2: List[T]) -> bool:
    # 创建两个计数字典，统计每个元素的出现次数
    count_l1 = {}
    count_l2 = {}

    # 统计 l1 中每个元素的出现次数
    for item in l1:
        if item in count_l1:
            count_l1[item] += 1
        else:
            count_l1[item] = 1

    # 统计 l2 中每个元素的出现次数
    for item in l2:
        if item in count_l2:
            count_l2[item] += 1
        else:
            count_l2[item] = 1

    # 检查 l1 中每个元素的出现次数是否大于等于 l2 中的出现次数
    for item, count in count_l2.items():
        if item not in count_l1 or count_l1[item] < count:
            return False

    return True


if __name__ == '__main__':
    # print(indent("HEAD 1\n\thead 2\n"))
    print(pairing_dicts_values(
        {"a": 1, "b": 1, "c": 2},
        {"A": 1, "B": 1, "C": 2, "D": 2},
    ))
