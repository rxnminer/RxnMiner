from __future__ import annotations
import math
import abc
import copy
from pathlib import Path
from dataclasses import dataclass, field
from functools import cached_property
from typing import Tuple, Optional, Sequence, NoReturn, List

import numpy as np
from lxml import etree
from nodejs import node

from ..constants import JS_CONST


def _raise_type_error(arg) -> NoReturn:
    raise TypeError(f'Unsupported argument type `{arg.__class__.__name__}`')


@dataclass(frozen=True)
class Point:
    X: float
    Y: float
    
    def distance(self, other) -> float:
        if isinstance(other, Point):
            return math.sqrt((self.X - other.X)**2 + (self.Y - other.Y)**2)
        elif isinstance(other, Line):
            return (
                abs(other.A * self.X + other.B * self.Y + other.C) / 
                math.sqrt(other.A ** 2 + other.B ** 2)
            )
        else:
            _raise_type_error(other)
            
    def transform(self, rotate: float, dx: float, dy: float) -> Point:
        """Get SVG compatible coordinates after rotation and translation."""
        sin, cos = math.sin(math.radians(rotate)), math.cos(math.radians(rotate))
        return Point(
            X=(self.X * cos - self.Y * sin + dx), 
            Y=(self.X * sin + self.Y * cos + dy)
        )
        
    def rotate(self, other: Point, angle: float, ccw_as_positive: bool = True) -> Point:
        """Get the new point yielded by rotation w.r.t point `other`."""
        a = math.radians(angle)
        sin, cos = math.sin, math.cos
        if ccw_as_positive:
            rX = (self.X - other.X) * cos(a) - (self.Y - other.Y) * sin(a) + other.X
            rY = (self.X - other.X) * sin(a) + (self.Y - other.Y) * cos(a) + other.Y
        else:
            rX = (self.X - other.X) * cos(a) + (self.Y - other.Y) * sin(a) + other.X
            rY = (self.X - other.X) * sin(a) - (self.Y - other.Y) * cos(a) + other.Y
        
        return Point(rX, rY)
        

def vec(p1: Point, p2: Point) -> Tuple[float, float]:
    return (p2.X - p1.X, p2.Y - p1.Y)


def dot(v1: Tuple[float, float], v2: Tuple[float, float]) -> float:
    return v1[0]*v2[0] + v1[1]*v2[1]


def norm(vec: Tuple[float, float]) -> float:
    return math.sqrt(vec[0]**2 + vec[1]**2)


def calc_vec_angle(v1: Tuple[float, float], v2: Tuple[float, float]) -> float:
    """Calculate the angle between two vectors."""
    cos_theta = dot(v1, v2) / norm(v1) / norm(v2)
    cos_theta = max(min(cos_theta, 1), -1)
    return math.degrees(math.acos(cos_theta))


def calc_vec_degree(vec: Tuple[float, float]) -> float:
    """Calculate the angle between input vector and postive x axis.
    Output ranges in (-180.0, 180.0].
    """
    x, y = vec
    cos_theta = x / norm(vec)
    cos_theta = max(min(cos_theta, 1), -1)
    deg = math.degrees(math.acos(cos_theta))
    if y < 0:
        return -deg
    return deg


def calc_centre_point(points: Sequence[Point]) -> Point:
    x = y = n = 0
    for p in points:
        x += p.X
        y += p.Y
        n += 1
    return Point(X=x/n, Y=y/n)
 

@dataclass(frozen=True)
class Line:
    A: float
    B: float
    C: float
    
    @classmethod
    def from_points(cls, p1: Point, p2: Point) -> Line:
        return cls(
            A=(p2.Y - p1.Y),
            B=(p1.X - p2.X),
            C=(p2.X * p1.Y - p1.X * p2.Y)
        )
        
    def distance(self, other) -> float:
        if isinstance(other, Point):
            return other.distance(self)
        else:
            _raise_type_error(other)
            
    def intersection(self, other: Line) -> Optional[Point]:
        """Compute the intersection point of two lines."""
        det = self.A * other.B - other.A * self.B
        if det == 0:
            return  # lines are parallel
        return Point(
            X=(other.C * self.B - self.C * other.B) / det,
            Y=(self.C * other.A - other.C * self.A) / det
        )
        
    def perpendicular_and_passes(self, p: Point) -> Line:
        """Compute the line that is perpendicular to `self` and passes Point `p`."""
        return Line(A=self.B, B=-self.A, C=(self.A * p.Y - self.B * p.X))
    

class Geometry(abc.ABC):
    """An abstract base class for stroing geometries in SVG canvas."""
    @abc.abstractmethod
    def intersects(self, other) -> bool:
        pass
    
    
@dataclass
class BoundingBox(Geometry):
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    
    def __post_init__(self) -> None:
        self.w = self.x_max - self.x_min
        self.h = self.y_max - self.y_min
    
    def intersects(self, other) -> bool:
        if isinstance(other, BoundingBox):
            return not (
                self.x_min > other.x_max or
                self.x_max < other.x_min or
                self.y_min > other.y_max or
                self.y_max < other.y_min
            )
        elif isinstance(other, Circle):
            # https://www.zhihu.com/question/31763307/answer/53280838
            return (
                other.x >= self.x_min - other.r and
                other.x <= self.x_max + other.r and
                other.y >= self.y_min - other.r and
                other.y <= self.y_max + other.r
            )
        elif isinstance(other, Segment):
            # https://www.zhihu.com/question/31763307/answer/53280838
            if (
                self.x_min <= other.p1.X <= self.x_max and
                self.y_min <= other.p1.Y <= self.y_max or
                self.x_min <= other.p2.X <= self.x_max and
                self.y_min <= other.p2.Y <= self.y_max
            ):
                # either end of segment is inside the bbox
                return True
            p_topleft = Point(self.x_min, self.y_min)
            p_botright = Point(self.x_max, self.y_max)
            p_topright = Point(self.x_max, self.y_min)
            p_botleft = Point(self.x_min, self.y_max)
            seg_d1 = Segment(p1=p_topleft, p2=p_botright)
            seg_d2 = Segment(p1=p_topright, p2=p_botleft)
            return other.intersects(seg_d1) or other.intersects(seg_d2)
        else:
            _raise_type_error(other)
    
    
@dataclass
class Circle(Geometry):
    x: float
    y: float
    r: float
    ring_sub_slots: List[int] = field(init=False)
    
    def __post_init__(self) -> None:
        self.ring_sub_slots = list(range(-150, 210, 30))  # 12 candidates in total

    def intersects(self, other) -> bool:
        if isinstance(other, BoundingBox):
            return other.intersects(self)
        elif isinstance(other, Circle):
            c1, c2 = Point(self.x, self.y), Point(other.x, other.y)
            return c1.distance(c2) <= self.r + other.r
        elif isinstance(other, Segment):
            # https://blog.csdn.net/SongBai1997/article/details/86599879
            centre = Point(self.x, self.y)
            if (
                centre.distance(other.p1) <= self.r or
                centre.distance(other.p2) <= self.r
            ):
                # either end of segment in circle -> intersects
                return True
            line = Line.from_points(other.p1, other.p2)
            if line.distance(centre) > self.r:
                return False
            # Compute vector angles.
            if (
                np.dot(vec(other.p1, centre), vec(other.p1, other.p2)) > 0 and
                np.dot(vec(other.p2, centre), vec(other.p2, other.p1)) > 0
            ):
                return True
            return False
        else:
            _raise_type_error(other)
    

@dataclass
class Segment(Geometry):
    p1: Point
    p2: Point
    
    @cached_property
    def length(self) -> float:
        return self.p1.distance(self.p2)
    
    def transform(self, rotate: float, dx: float, dy: float) -> Segment:
        """Get SVG compatible coordinates after rotation and translation."""
        return Segment(
            p1=self.p1.transform(rotate=rotate, dx=dx, dy=dy),
            p2=self.p2.transform(rotate=rotate, dx=dx, dy=dy)
        )
            
    def intersects(self, other) -> bool:
        if isinstance(other, Segment):
            # https://zhuanlan.zhihu.com/p/566208007
            # https://www.cnblogs.com/MinPage/p/14044836.html
            # fast exclusion trial
            if (
                max(self.p1.X, self.p2.X) < min(other.p1.X, other.p2.X) or
                max(other.p1.X, other.p2.X) < min(self.p1.X, self.p2.X) or
                max(self.p1.Y, self.p2.Y) < min(other.p1.Y, other.p2.Y) or
                max(other.p1.Y, other.p2.Y) < min(self.p1.Y, self.p2.Y)
            ):
                return False
            # straddling trial
            seg_AD = vec(self.p1, other.p2)
            seg_BD = vec(self.p2, other.p2)
            seg_CD = vec(other.p1, other.p2)
            seg_AB = vec(self.p1, self.p2)
            seg_CB = vec(other.p1, self.p2)
            seg_DB = vec(other.p2, self.p2)
            if (
                np.cross(seg_AD, seg_CD) * np.cross(seg_BD, seg_CD) <= 0 and
                np.cross(seg_CB, seg_AB) * np.cross(seg_DB, seg_AB) <= 0
            ):
                return True
            return False
        elif isinstance(other, (BoundingBox, Circle)):
            return other.intersects(self)
        else:
            _raise_type_error(other)
            
    def reverse(self) -> Segment:
        return Segment(self.p2, self.p1)
            
            
class SVGBboxFetcher(abc.ABC):
    """An abstract base class for calculating SVG element bounding box."""
    render_script = str(Path(__file__).parent.parent.joinpath('bbox.js').absolute())
    
    def __init__(self) -> None:
        pass

    def _render_bbox(
        self, 
        # cmd: str, 
        cmd: List[str],
        svg: bytes, 
        padding: float = 0.0,
        **kwds
    ) -> BoundingBox:
        """Call Node.js to render for bounding box."""
        p = node.run(cmd, input=svg.decode(), text=True, capture_output=True)
        if p.returncode == 0 and p.stdout.startswith(JS_CONST['success_header']):
            x, y, w, h = p.stdout.split(' ')[1:]
            h = h.strip()  # remove `\n`
            x, y, w, h = tuple(map(float, (x, y, w, h)))
            x_mid, y_mid = x + w/2, y + h/2
            w, h = w + padding, h + padding
            x_min, y_min = x_mid - w/2, y_mid - h/2
            return BoundingBox(
                x_min=x_min, y_min=y_min, x_max=x_min+w, y_max=y_min+h
            )
        else:
            raise RuntimeError(f'Rendering failed due to:\n{p.stderr}')
        
        
class SVGPathBboxFetcher(SVGBboxFetcher):
    """Render SVG <path> element and get its bounding box."""    
    def __call__(
        self, 
        elements: Sequence[etree._Element], 
        viewbox: str,
        **kwds
    ) -> BoundingBox:
        assert len(elements) > 0
        nsmap = elements[0].nsmap
        root = etree.Element('svg', nsmap=nsmap, viewbox=viewbox)
        for elem in elements:
            # It is safe to copy elements as they do not have children.
            root.append(copy.deepcopy(elem))
        cmd = [self.__class__.render_script]
        return super()._render_bbox(
            cmd=cmd, svg=etree.tostring(root, pretty_print=True), **kwds
        )

        
class SVGTextBboxFetcher(SVGBboxFetcher):
    """Render SVG <text> element and get its bounding box."""
    def __call__(
        self,
        text: etree._Element, 
        style: etree._Element,
        viewbox: str,
        font: str,
        font_file: str,
        **kwds
    ) -> BoundingBox:
        root = etree.Element('svg', nsmap=text.nsmap, viewbox=viewbox)
        root.append(copy.deepcopy(style))
        root.append(copy.deepcopy(text))
        cmd = [
            self.__class__.render_script,
            '--font', font,
            '--font-file', font_file,
        ]
        return super()._render_bbox(
            cmd, svg=etree.tostring(root, pretty_print=True), **kwds
        )